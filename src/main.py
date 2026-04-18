"""Telegram Bot 主程序"""

import asyncio
import sys

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiohttp import ClientConnectionError, ServerDisconnectedError
from loguru import logger
from sentry_sdk.integrations.loguru import LoguruIntegration

from src.core.config import settings
from src.core.database import close_db, init_db
from src.core.executor import shutdown_executor  # ✅ P1-11: 导入线程池关闭函数
from src.core.redis import close_redis
from src.core.telethon_client import close_telethon_client, init_telethon_client

# 定义需要过滤的网络临时性错误类型（用于 Sentry 过滤和异常处理）
NETWORK_ERROR_TYPES = (
    TelegramNetworkError,  # Telegram API 网络错误（包含所有子类）
    TelegramRetryAfter,  # 速率限制（429 Too Many Requests）
    ClientConnectionError,  # aiohttp 连接错误（包含 ClientConnectorError）
    ServerDisconnectedError,  # 服务器断开连接
    TimeoutError,  # 超时错误（内置异常）
    ConnectionError,  # 通用连接错误（内置异常，包含 ConnectionResetError 等）
)


def before_send(event, hint):
    """Sentry 事件发送前的数据清理钩子，过滤敏感信息和临时性错误"""
    import re

    # 1. 过滤网络临时性错误（自动重试的错误不需要告警）
    # 优先使用 hint 中的原始异常信息（更准确）
    if "exc_info" in hint:
        exc = hint["exc_info"][1]
        if isinstance(exc, NETWORK_ERROR_TYPES):
            return None

    # 2. 定义敏感数据的正则模式
    token_pattern = re.compile(r"\d+:[A-Za-z0-9_-]{35}")  # Telegram Bot Token 格式
    url_token_pattern = re.compile(r"bot(\d+:[A-Za-z0-9_-]{35})")  # URL 中的 token

    def scrub_sensitive_data(data):
        """递归清理敏感数据"""
        if isinstance(data, dict):
            return {key: scrub_sensitive_data(value) for key, value in data.items()}
        elif isinstance(data, list):
            return [scrub_sensitive_data(item) for item in data]
        elif isinstance(data, str):
            # 替换 Bot Token
            data = token_pattern.sub("[FILTERED_BOT_TOKEN]", data)
            data = url_token_pattern.sub("bot[FILTERED_BOT_TOKEN]", data)
            return data
        return data

    # 3. 清理事件数据中的敏感信息
    if "exception" in event:
        event["exception"] = scrub_sensitive_data(event["exception"])

    if "message" in event:
        event["message"] = scrub_sensitive_data(event["message"])

    if "breadcrumbs" in event:
        event["breadcrumbs"] = scrub_sensitive_data(event["breadcrumbs"])

    if "request" in event:
        event["request"] = scrub_sensitive_data(event["request"])

    if "extra" in event:
        event["extra"] = scrub_sensitive_data(event["extra"])

    return event


async def setup_bot() -> tuple[Bot, Dispatcher]:
    """初始化 Bot 和 Dispatcher"""
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # ✅ 注册 Session 层中间件：处理 Telegram API 速率限制 (429)
    from src.bot.middlewares import RetryAfterMiddleware

    bot.session.middleware(RetryAfterMiddleware(max_retries=3))

    dp = Dispatcher()

    # 注册路由器
    from src.bot.handlers import admin, antispam, cleanup, events, moderation, start, verification

    dp.include_router(events.router)  # 系统事件（最高优先级）
    dp.include_router(start.router)  # 启动命令
    dp.include_router(admin.router)  # 管理命令
    dp.include_router(cleanup.router)  # 清理命令
    dp.include_router(moderation.router)  # 群管理命令
    dp.include_router(verification.router)  # 入群验证
    dp.include_router(antispam.router)  # 反垃圾检测（放在最后）

    # ✅ 自动提取所有已注册的命令并设置到反垃圾白名单
    from aiogram.filters import Command

    def extract_commands_from_router(router) -> set[str]:
        """从 router 中提取所有已注册的命令"""
        commands: set[str] = set()
        for handler in router.message.handlers:
            for filter_obj in handler.filters:
                # filter_obj.callback 是实际的 Command 对象
                if isinstance(filter_obj.callback, Command):
                    # 过滤掉正则表达式，只保留字符串命令
                    commands.update(c for c in filter_obj.callback.commands if isinstance(c, str))
        return commands

    # 从 dispatcher 和所有 sub routers 中提取命令
    registered_commands = set()
    registered_commands.update(extract_commands_from_router(dp))
    for router in dp.sub_routers:
        registered_commands.update(extract_commands_from_router(router))

    # 设置到反垃圾模块
    antispam.set_registered_commands(registered_commands)

    # ✅ 注册白名单中间件（最高优先级）
    from src.bot.middlewares import WhitelistMiddleware

    dp.message.middleware(WhitelistMiddleware())
    dp.callback_query.middleware(WhitelistMiddleware())

    # ✅ 注册速率限制中间件
    # 说明：
    # - 消息不做速率限制，全部交由反垃圾系统处理，检测为垃圾后直接封禁/禁言，比简单限流更有效
    # - 仅对 callback_query 启用速率限制，用于防止按钮刷点/恶意点击
    from src.bot.middlewares import ThrottleMiddleware

    dp.callback_query.middleware(ThrottleMiddleware(rate_limit=5, time_window=1))

    # ✅ 注册自动删除中间件（在群组中自动删除命令消息和响应）
    from src.bot.middlewares import AutoDeleteMiddleware

    dp.message.middleware(AutoDeleteMiddleware(response_delay=30))

    # ✅ 注册 CAS 黑名单检查中间件（在白名单和限流之后，handler 之前）
    if settings.cas_enabled:
        from src.bot.middlewares import CASCheckMiddleware

        dp.message.middleware(CASCheckMiddleware())

    # ✅ 注册全局错误处理器（过滤网络临时性错误）
    from aiogram.types import ErrorEvent

    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        """全局错误处理器：网络错误仅记录日志，其他错误上报 Sentry"""
        exception = event.exception

        # 网络临时性错误：仅记录日志，不上报 Sentry
        if isinstance(
            exception, (TelegramNetworkError, ClientConnectionError, ServerDisconnectedError)
        ):
            logger.warning(f"Handler 网络错误: {type(exception).__name__}: {exception}")
            return True  # 标记为已处理，不再传播

        # 其他异常：上报 Sentry
        logger.exception(f"Handler 异常: {type(exception).__name__}: {exception}")
        sentry_sdk.capture_exception(exception)
        return True  # 标记为已处理，避免 aiogram 默认处理

    return bot, dp


async def setup_bot_commands(bot: Bot) -> None:
    """设置 Bot 命令自动完成提示"""
    from aiogram.types import (
        BotCommand,
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
    )

    # 私聊命令列表（普通用户 + 超级管理员命令）
    private_commands = [
        # 普通命令
        BotCommand(command="start", description="启动 Bot / 查看帮助"),
        BotCommand(command="help", description="查看帮助信息"),
        # 超级管理员命令
        BotCommand(command="health", description="健康检查（仅超管）"),
        BotCommand(command="stats", description="统计信息（仅超管）"),
        BotCommand(command="whitelist", description="白名单管理（仅超管）"),
    ]

    # 群组普通成员命令列表（仅基础功能）
    group_member_commands = [
        BotCommand(command="help", description="查看帮助信息"),
        BotCommand(command="spam", description="举报垃圾消息"),
        BotCommand(command="report", description="举报垃圾消息"),
    ]

    # 群组管理员命令列表（完整管理功能）
    group_admin_commands = [
        # 群组配置
        BotCommand(command="groupset", description="⚙️ 群组设置（统一入口）"),
        BotCommand(command="setverify", description="设置验证方式"),
        BotCommand(command="settimeout", description="设置验证超时时间"),
        BotCommand(command="verifyconfig", description="查看验证配置"),
        BotCommand(command="antispam", description="反垃圾配置"),
        BotCommand(command="antichannel", description="反频道马甲配置"),
        BotCommand(command="activity", description="活跃度系统开关"),
        BotCommand(command="activityskip", description="活跃度跳过阈值"),
        # 群成员管理
        BotCommand(command="kick", description="踢出成员"),
        BotCommand(command="mute", description="禁言成员"),
        BotCommand(command="unmute", description="解除禁言"),
        BotCommand(command="ban", description="封禁成员"),
        BotCommand(command="unban", description="解除封禁"),
        BotCommand(command="warn", description="警告成员"),
        BotCommand(command="warnings", description="查看警告记录"),
        BotCommand(command="clearwarnings", description="清除警告"),
        BotCommand(command="cleanup", description="清理不活跃用户"),
        # 消息管理
        BotCommand(command="delbefore", description="删除往前的消息"),
        BotCommand(command="delafter", description="删除往后的消息"),
        BotCommand(command="delrange", description="删除消息范围"),
        # 举报系统
        BotCommand(command="spam", description="举报垃圾消息"),
        BotCommand(command="report", description="举报垃圾消息"),
        BotCommand(command="notspam", description="标记非垃圾消息"),
        BotCommand(command="nospam", description="标记非垃圾消息"),
        BotCommand(command="unspam", description="标记非垃圾消息"),
        BotCommand(command="reports", description="查看举报列表"),
        BotCommand(command="approve", description="处理举报"),
        # 帮助
        BotCommand(command="help", description="查看帮助信息"),
    ]

    try:
        # 设置私聊命令
        await bot.set_my_commands(
            commands=private_commands,
            scope=BotCommandScopeAllPrivateChats(),
        )
        logger.info(f"✅ 已设置私聊命令列表 ({len(private_commands)} 个命令)")

        # 设置群组普通成员命令（优先级较低）
        await bot.set_my_commands(
            commands=group_member_commands,
            scope=BotCommandScopeAllGroupChats(),
        )
        logger.info(f"✅ 已设置群组普通成员命令列表 ({len(group_member_commands)} 个命令)")

        # 设置群组管理员命令（优先级较高，会覆盖普通成员的命令）
        await bot.set_my_commands(
            commands=group_admin_commands,
            scope=BotCommandScopeAllChatAdministrators(),
        )
        logger.info(f"✅ 已设置群组管理员命令列表 ({len(group_admin_commands)} 个命令)")

    except Exception as e:
        logger.error(f"设置命令列表失败: {e}")


async def on_startup(bot: Bot) -> None:
    """启动时执行"""
    logger.info("Bot 正在启动...")

    # 初始化数据库
    logger.info("初始化数据库...")
    await init_db()

    # 初始化 Telethon 客户端
    logger.info("初始化 Telethon 客户端...")
    await init_telethon_client()

    # 初始化健康检查器（记录启动时间）
    from src.core.health import get_health_checker

    get_health_checker()
    logger.info("健康检查器已初始化")

    # 设置命令自动完成
    logger.info("设置命令自动完成...")
    await setup_bot_commands(bot)

    logger.info("Bot 启动完成")


async def on_shutdown() -> None:
    """关闭时执行"""
    logger.info("Bot 正在关闭...")

    # ✅ 关闭 AI 检测器（修复 Event loop is closed 错误）
    try:
        from src.ml.ai_detector import get_ai_detector

        await get_ai_detector().close()
        logger.info("✅ AI 检测器已关闭")
    except Exception as e:
        logger.warning(f"关闭 AI 检测器失败: {e}")

    # ✅ 关闭 CAS 客户端
    try:
        from src.services.cas_service import get_cas_service

        await get_cas_service().close()
        logger.info("✅ CAS 客户端已关闭")
    except Exception as e:
        logger.warning(f"关闭 CAS 客户端失败: {e}")

    # ✅ P1-11: 关闭线程池
    shutdown_executor(wait=True)

    # 关闭 Telethon 客户端
    await close_telethon_client()

    # 关闭数据库连接
    await close_db()

    # 关闭 Redis 连接
    await close_redis()

    logger.info("Bot 已关闭")


async def main() -> None:
    """主函数"""
    # 初始化 Sentry（如果配置了 DSN）
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            # 集成 Loguru 日志
            integrations=[
                LoguruIntegration(
                    level=None,  # 捕获所有 Loguru 日志级别
                    event_level=40,  # 仅 ERROR 及以上级别创建 Sentry 事件 (40=ERROR)
                )
            ],
            # 发布版本（使用项目版本）
            release="tg-guard-bot@0.1.0",
            # 附加上下文
            attach_stacktrace=True,
            # 过滤敏感数据
            send_default_pii=False,
            # 自定义事件过滤钩子（过滤 Bot Token）
            before_send=before_send,
            # 默认的敏感字段名称过滤
            # Sentry 会自动过滤包含这些关键词的字段
            _experiments={  # type: ignore[typeddict-unknown-key]
                "profiles_sample_rate": 0,  # 禁用 profiling
            },
        )
        logger.info(f"✅ Sentry 已初始化 (环境: {settings.sentry_environment})")
        logger.info("🔒 已启用敏感数据过滤（Bot Token 将被自动屏蔽）")
        logger.info("🚫 已启用网络错误过滤（临时性网络错误不会发送到 Sentry）")
    else:
        logger.info("⚠️ Sentry DSN 未配置，跳过 Sentry 初始化")

    # 配置日志
    # 移除默认的 stderr handler
    logger.remove()

    # 添加控制台输出（彩色，仅 INFO 及以上）
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=settings.log_level,
        colorize=True,
    )

    # 添加文件输出（使用配置的日志级别，生产环境避免记录 DEBUG）
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",  # 每天午夜轮转
        retention="7 days",  # 保留 7 天
        compression="zip",  # 压缩旧日志
        level=settings.log_level,  # ✅ 安全修复：使用配置的日志级别而非固定 DEBUG
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    # 添加错误日志文件（仅 ERROR 及以上）
    logger.add(
        "logs/error_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",  # 错误日志保留 30 天
        compression="zip",
        level="ERROR",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    # 添加 JSON 格式日志（用于日志分析）
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.json",
        rotation="00:00",
        retention="7 days",
        compression="zip",
        level="INFO",
        encoding="utf-8",
        serialize=True,  # JSON 格式
    )

    # 初始化 Bot 和 Dispatcher
    bot, dp = await setup_bot()

    # 启动回调
    await on_startup(bot)

    # 启动轮询（无限重试，直到成功或手动停止）
    retry_delay = 5.0
    max_delay = 60.0
    attempt = 0

    while True:
        attempt += 1
        try:
            # 启动轮询
            logger.info(f"开始轮询... (尝试 {attempt})")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break  # 正常退出（通常不会到达这里，因为 polling 会一直运行）

        except (KeyboardInterrupt, SystemExit):
            logger.info("收到停止信号，正在退出...")
            break

        except NETWORK_ERROR_TYPES as e:
            # 网络临时性错误，仅记录日志，不上报 Sentry
            logger.warning(f"网络错误 (尝试 {attempt}): {type(e).__name__}: {e}")
            logger.info(f"等待 {retry_delay:.1f} 秒后重试...")
            await asyncio.sleep(retry_delay)
            # 指数退避，最大 60 秒
            retry_delay = min(retry_delay * 1.5, max_delay)

        except Exception as e:
            # 其他异常上报 Sentry
            logger.error(f"启动轮询失败 (尝试 {attempt}): {type(e).__name__}: {e}")
            sentry_sdk.capture_exception(e)
            logger.warning(f"等待 {retry_delay:.1f} 秒后重试...")
            await asyncio.sleep(retry_delay)
            # 指数退避，最大 60 秒
            retry_delay = min(retry_delay * 1.5, max_delay)

    # 关闭回调
    await on_shutdown()
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
