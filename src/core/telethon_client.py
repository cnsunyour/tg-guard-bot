"""Telethon 客户端管理模块"""

from pathlib import Path

from loguru import logger
from telethon import TelegramClient

from src.core.config import settings
from src.core.proxy import get_telethon_proxy

# 全局 Telethon 客户端
_telethon_client: TelegramClient | None = None


async def init_telethon_client() -> TelegramClient | None:
    """初始化 Telethon 客户端

    Returns:
        TelegramClient 实例，如果未启用或配置不完整则返回 None
    """
    global _telethon_client

    # 检查是否启用
    if not settings.telethon_enabled:
        logger.info("Telethon 客户端未启用")
        return None

    # 检查配置
    if not settings.telethon_api_id or not settings.telethon_api_hash:
        logger.warning("Telethon 配置不完整，跳过初始化")
        return None

    # 检查 session 文件
    session_path = Path(settings.telethon_session_path)
    if not session_path.exists():
        logger.warning(
            f"Telethon session 文件不存在: {session_path}\n"
            "请先运行 scripts/telethon_login.py 生成 session 文件"
        )
        return None

    # 检查是否是文件而不是目录（Docker 挂载不存在的文件会创建目录）
    if not session_path.is_file():
        logger.warning(
            f"Telethon session 路径不是文件: {session_path}\n"
            "可能是 Docker 挂载导致的目录，请确保在宿主机生成 session 文件后再启动容器"
        )
        return None

    try:
        # 创建客户端
        # session_path 去掉 .session 后缀
        session_name = str(session_path.with_suffix(""))

        # 检测代理配置
        proxy = get_telethon_proxy()

        _telethon_client = TelegramClient(
            session_name,
            settings.telethon_api_id,
            settings.telethon_api_hash,
            proxy=proxy,
        )

        # 启动客户端
        await _telethon_client.start()

        # 获取当前用户信息
        me = await _telethon_client.get_me()
        logger.info(f"Telethon 客户端已启动: {me.first_name} (@{me.username or 'N/A'})")

        return _telethon_client

    except Exception as e:
        logger.error(f"Telethon 客户端初始化失败: {e}")
        _telethon_client = None
        return None


def get_telethon_client() -> TelegramClient | None:
    """获取 Telethon 客户端实例

    Returns:
        TelegramClient 实例，如果未初始化则返回 None
    """
    return _telethon_client


async def close_telethon_client() -> None:
    """关闭 Telethon 客户端"""
    global _telethon_client
    if _telethon_client:
        await _telethon_client.disconnect()
        logger.info("Telethon 客户端已关闭")
        _telethon_client = None
