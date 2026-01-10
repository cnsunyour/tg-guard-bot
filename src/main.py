"""Telegram Bot 主程序"""

import asyncio
import sys

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from loguru import logger
from sentry_sdk.integrations.loguru import LoguruIntegration

from src.core.config import settings
from src.core.database import close_db, init_db
from src.core.executor import shutdown_executor  # ✅ P1-11: 导入线程池关闭函数
from src.core.redis import close_redis


def before_send(event, _hint):
    """Sentry 事件发送前的数据清理钩子，过滤敏感信息和临时性错误"""
    import re

    # 1. 过滤网络临时性错误（自动重试的错误不需要告警）
    if "exception" in event and "values" in event["exception"]:
        for exc_value in event["exception"]["values"]:
            exc_type = exc_value.get("type", "")
            exc_module = exc_value.get("module", "")

            # 网络相关的临时性错误
            network_errors = [
                "TelegramNetworkError",  # Telegram 网络错误
                "ClientConnectorError",  # aiohttp 连接错误
                "ServerDisconnectedError",  # 服务器断开连接
                "TimeoutError",  # 超时错误
                "asyncio.TimeoutError",  # asyncio 超时
                "ConnectionError",  # 连接错误
                "ConnectionResetError",  # 连接重置
                "BrokenPipeError",  # 管道破裂
                "OSError",  # 操作系统错误（网络相关）
            ]

            # 检查是否是网络错误
            if exc_type in network_errors:
                # 返回 None 表示丢弃该事件，不发送到 Sentry
                return None

            # 检查是否是 aiogram 的可恢复错误
            if exc_module and "aiogram" in exc_module:
                if "RestartingTelegram" in exc_type or "RetryAfter" in exc_type:
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
    from src.bot.handlers import admin, antispam, events, moderation, start, verification

    dp.include_router(events.router)  # 系统事件（最高优先级）
    dp.include_router(start.router)  # 启动命令
    dp.include_router(admin.router)  # 管理命令
    dp.include_router(moderation.router)  # 群管理命令
    dp.include_router(verification.router)  # 入群验证
    dp.include_router(antispam.router)  # 反垃圾检测（放在最后）

    # ✅ 自动提取所有已注册的命令并设置到反垃圾白名单
    from aiogram.filters import Command

    def extract_commands_from_router(router) -> set[str]:
        """从 router 中提取所有已注册的命令"""
        commands = set()
        for handler in router.message.handlers:
            for filter_obj in handler.filters:
                # filter_obj.callback 是实际的 Command 对象
                if isinstance(filter_obj.callback, Command):
                    commands.update(filter_obj.callback.commands)
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

    # ✅ 注册速率限制中间件（防止 DoS 攻击）
    from src.bot.middlewares import ThrottleMiddleware

    dp.message.middleware(ThrottleMiddleware(rate_limit=3, time_window=1))
    dp.callback_query.middleware(ThrottleMiddleware(rate_limit=5, time_window=1))

    # ✅ 注册自动删除中间件（在群组中自动删除命令消息和响应）
    from src.bot.middlewares import AutoDeleteMiddleware

    dp.message.middleware(AutoDeleteMiddleware(response_delay=30))

    return bot, dp


async def on_startup() -> None:
    """启动时执行"""
    logger.info("Bot 正在启动...")

    # 初始化数据库
    logger.info("初始化数据库...")
    await init_db()

    logger.info("Bot 启动完成")


async def on_shutdown() -> None:
    """关闭时执行"""
    logger.info("Bot 正在关闭...")

    # ✅ P1-11: 关闭线程池
    shutdown_executor(wait=True)

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
                    event_level="ERROR",  # 仅 ERROR 及以上级别创建 Sentry 事件
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
            _experiments={
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
    await on_startup()

    try:
        # 启动轮询
        logger.info("开始轮询...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到停止信号")
    finally:
        # 关闭回调
        await on_shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
