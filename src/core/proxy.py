"""代理配置工具模块

用于检测和解析环境变量中的代理配置，支持 socks5, socks4, http 三种代理类型。

依赖:
    - python-socks[asyncio]: Telethon 使用代理时需要此依赖
      安装: pip install python-socks[asyncio]
"""

import os
from typing import Tuple
from urllib.parse import urlparse

from loguru import logger


def get_proxy_config() -> Tuple[str, str, int] | None:
    """从环境变量中获取代理配置

    优先级：
    1. socks5_proxy / SOCKS5_PROXY
    2. socks_proxy / SOCKS_PROXY
    3. all_proxy / ALL_PROXY
    4. https_proxy / HTTPS_PROXY
    5. http_proxy / HTTP_PROXY

    Returns:
        (proxy_type, host, port) 元组，如果没有配置则返回 None
        proxy_type 可以是 "socks5", "socks4", "http"

    Examples:
        支持的代理 URL 格式：
        - socks5://127.0.0.1:7891
        - socks5://user:pass@127.0.0.1:7891
        - http://proxy.example.com:8080
        - http://user:pass@proxy.example.com:8080
    """
    # 按优先级检查环境变量
    proxy_env_vars = [
        "socks5_proxy",
        "SOCKS5_PROXY",
        "socks_proxy",
        "SOCKS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
    ]

    for env_var in proxy_env_vars:
        proxy_url = os.getenv(env_var)
        if proxy_url:
            logger.debug(f"检测到代理环境变量 {env_var}={proxy_url}")
            proxy_config = _parse_proxy_url(proxy_url)
            if proxy_config:
                proxy_type, host, port = proxy_config
                logger.info(f"使用代理: {proxy_type}://{host}:{port}")
                return proxy_config
            else:
                logger.warning(f"无法解析代理 URL: {proxy_url}")

    logger.debug("未检测到代理配置")
    return None


def _parse_proxy_url(proxy_url: str) -> Tuple[str, str, int] | None:
    """解析代理 URL

    Args:
        proxy_url: 代理 URL (如 socks5://127.0.0.1:7891)

    Returns:
        (proxy_type, host, port) 元组，解析失败返回 None
    """
    try:
        # 解析 URL
        parsed = urlparse(proxy_url)

        # 获取协议类型
        scheme = parsed.scheme.lower()
        if not scheme:
            # 没有协议前缀，尝试直接解析 host:port
            if ":" in proxy_url:
                parts = proxy_url.rsplit(":", 1)
                return ("socks5", parts[0], int(parts[1]))
            return None

        # 映射协议类型
        if scheme in ("socks5", "socks5h"):
            proxy_type = "socks5"
        elif scheme in ("socks4", "socks4a"):
            proxy_type = "socks4"
        elif scheme in ("http", "https"):
            proxy_type = "http"
        else:
            logger.warning(f"不支持的代理协议: {scheme}")
            return None

        # 获取主机和端口
        host = parsed.hostname
        port = parsed.port

        if not host:
            logger.warning(f"代理 URL 缺少主机名: {proxy_url}")
            return None

        # 默认端口
        if not port:
            if proxy_type in ("socks5", "socks4"):
                port = 1080
            elif proxy_type == "http":
                port = 8080
            logger.debug(f"使用默认端口: {port}")

        return (proxy_type, host, port)

    except Exception as e:
        logger.warning(f"解析代理 URL 失败 {proxy_url}: {e}")
        return None


def get_telethon_proxy() -> Tuple[str, str, int] | None:
    """获取 Telethon 客户端的代理配置

    Returns:
        Telethon 格式的代理元组 (type, host, port)，没有配置则返回 None
    """
    return get_proxy_config()
