"""代理配置工具模块

用于检测和解析环境变量中的代理配置，支持 socks5, socks4, http 三种代理类型。

依赖:
    - python-socks[asyncio]: Telethon 使用代理时需要此依赖
      安装: pip install python-socks[asyncio]
"""

import os
from urllib.parse import urlparse

from loguru import logger

# 代理协议缺失端口时的默认端口（与 _parse_proxy_url 保持一致）
_DEFAULT_PROXY_PORTS: dict[str, int] = {
    "socks5": 1080,
    "socks5h": 1080,
    "socks4": 1080,
    "socks4a": 1080,
    "http": 8080,
    "https": 8080,
}


def _proxy_has_auth(raw: str) -> bool:
    """判断代理 URL 是否含 userinfo（只返回布尔，不暴露 user/password 内容）。

    有协议时 urlparse 正确识别 userinfo；无协议（如 ``user@host:port``）时
    urlparse 不识别 userinfo，回退检测 ``@``（合法 host 不含 @，故 @ 是
    userinfo 存在的可靠信号）。
    """
    try:
        parsed = urlparse(raw)
        if parsed.username is not None or parsed.password is not None:
            return True
    except (TypeError, ValueError):
        pass
    return "@" in raw


def _redact_proxy_url(raw: str) -> str:
    """返回剥离 userinfo 的 ``scheme://host:port``，用于日志记录。

    无法安全解析时返回占位符 ``<unparseable>``，绝不回退到原始 URL，避免
    代理凭证（user:pass）进入日志。保持与 :func:`_parse_proxy_url` 一致的
    兼容行为：无协议的 ``host:port`` 视为 socks5；缺失端口时按协议默认端口补齐。
    """
    try:
        parsed = urlparse(raw)
        scheme = parsed.scheme.lower()
        host: str | None = None
        port: int | None = None

        # urlparse 会把无协议的 "user:pass@host:port" 中的 user: 误当 scheme；
        # 仅信任已知代理协议（或空），否则按无协议格式重新剥离 userinfo。
        if scheme not in {"", "socks5", "socks5h", "socks4", "socks4a", "http", "https"}:
            scheme = ""

        if not scheme:
            # 无协议：按现有兼容逻辑当作 socks5 host:port
            host, separator, port_text = raw.rpartition(":")
            if not separator or not host or not port_text.isdigit():
                return "<unparseable>"
            # 无协议但含 userinfo（user@host:port）→ 剥离 @ 前的凭证，只保留 host
            if "@" in host:
                host = host.rpartition("@")[2]
            scheme = "socks5"
            port = int(port_text)
        else:
            host = parsed.hostname
            port = parsed.port  # 非法端口会抛 ValueError
            if port is None:
                port = _DEFAULT_PROXY_PORTS.get(scheme)

        if not host or port is None:
            return "<unparseable>"
        if not 1 <= port <= 65535:
            return "<unparseable>"
        # 拒绝主机名含控制字符，防日志注入与异常显示
        if any(ord(ch) < 32 for ch in host):
            return "<unparseable>"

        # IPv6 主机加方括号，避免与端口分隔符混淆
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"{scheme}://{display_host}:{port}"
    except (TypeError, ValueError):
        return "<unparseable>"


def get_proxy_config() -> tuple[str, str, int] | None:
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

    Note:
        所有日志只记录脱敏后的 ``scheme://host:port`` 与 ``has_auth`` 布尔，
        绝不记录用户名/密码，避免代理凭证泄露到日志或 Sentry。
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
            redacted = _redact_proxy_url(proxy_url)
            has_auth = _proxy_has_auth(proxy_url)
            logger.debug(f"检测到代理环境变量 {env_var}={redacted} [has_auth={has_auth}]")
            proxy_config = _parse_proxy_url(proxy_url)
            if proxy_config:
                proxy_type, host, port = proxy_config
                logger.info(f"使用代理: {proxy_type}://{host}:{port} [has_auth={has_auth}]")
                return proxy_config
            else:
                logger.warning(f"无法解析代理 URL: {redacted} [has_auth={has_auth}]")

    logger.debug("未检测到代理配置")
    return None


def _parse_proxy_url(proxy_url: str) -> tuple[str, str, int] | None:
    """解析代理 URL

    Args:
        proxy_url: 代理 URL (如 socks5://127.0.0.1:7891)

    Returns:
        (proxy_type, host, port) 元组，解析失败返回 None

    Note:
        所有诊断日志均使用 :func:`_redact_proxy_url` 脱敏后的 URL，不含 userinfo。
    """
    redacted = _redact_proxy_url(proxy_url)
    has_auth = _proxy_has_auth(proxy_url)

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
            logger.warning(f"不支持的代理协议: {scheme} [proxy={redacted}] [has_auth={has_auth}]")
            return None

        # 获取主机和端口
        host = parsed.hostname
        port = parsed.port

        if not host:
            logger.warning(f"代理 URL 缺少主机名: {redacted} [has_auth={has_auth}]")
            return None

        # 默认端口
        if not port:
            if proxy_type in ("socks5", "socks4"):
                port = 1080
            elif proxy_type == "http":
                port = 8080
            logger.debug(f"使用默认端口: {port}")

        # 类型保护：确保 port 不为 None
        if port is None:
            logger.warning(f"无法确定代理端口: {redacted} [has_auth={has_auth}]")
            return None

        return (proxy_type, host, port)

    except Exception as e:
        # 只记录异常类型名，避免异常文本回含原始 URL（如 urlparse 的 ValueError）
        logger.warning(f"解析代理 URL 失败 {redacted} [has_auth={has_auth}]: {type(e).__name__}")
        return None


def get_telethon_proxy() -> tuple[str, str, int] | None:
    """获取 Telethon 客户端的代理配置

    Returns:
        Telethon 格式的代理元组 (type, host, port)，没有配置则返回 None
    """
    return get_proxy_config()
