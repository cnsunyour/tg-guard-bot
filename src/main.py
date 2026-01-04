"""Telegram Bot 主程序"""

import sys
import asyncio
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from loguru import logger

from src.core.config import settings
from src.core.database import init_db, close_db
from src.core.redis import close_redis
from src.core.executor import shutdown_executor  # ✅ P1-11: 导入线程池关闭函数


async def setup_bot() -> tuple[Bot, Dispatcher]:
    """初始化 Bot 和 Dispatcher"""
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # 注册路由器
    from src.bot.handlers import verification, admin, moderation, antispam, events
    dp.include_router(events.router)  # 系统事件（最高优先级）
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

    # 添加文件输出（所有级别）
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",  # 每天午夜轮转
        retention="7 days",  # 保留 7 天
        compression="zip",  # 压缩旧日志
        level="DEBUG",
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
