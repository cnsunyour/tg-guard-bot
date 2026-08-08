"""代理 URL 脱敏测试（M3 + codex review P1-1）。

验证 _redact_proxy_url 剥离 userinfo（含无协议 user@host:port 场景），
_proxy_has_auth 正确识别凭证存在性，确保代理凭证不进入日志。
"""

import pytest

from src.core.proxy import _proxy_has_auth, _redact_proxy_url

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 有协议 + userinfo
        ("socks5://user:pass@127.0.0.1:7891", "socks5://127.0.0.1:7891"),
        ("socks5://user@127.0.0.1:7891", "socks5://127.0.0.1:7891"),
        ("http://user:pass@proxy.example.com:8080", "http://proxy.example.com:8080"),
        # 有协议无 userinfo
        ("socks5://127.0.0.1:7891", "socks5://127.0.0.1:7891"),
        # 无协议 + userinfo（P1-1：user@host:port 必须剥离 user@）
        ("user@127.0.0.1:7891", "socks5://127.0.0.1:7891"),
        ("user:pass@127.0.0.1:7891", "socks5://127.0.0.1:7891"),
        # 无协议无 userinfo
        ("127.0.0.1:7891", "socks5://127.0.0.1:7891"),
    ],
)
def test_redact_proxy_url_strips_userinfo(raw: str, expected: str) -> None:
    assert _redact_proxy_url(raw) == expected


def test_redact_proxy_url_unparseable() -> None:
    """无法安全解析 → 占位符，绝不回退原始 URL。"""
    assert _redact_proxy_url("not a url at all") == "<unparseable>"
    assert _redact_proxy_url("") == "<unparseable>"


def test_redact_proxy_url_default_port() -> None:
    """缺失端口按协议默认补齐，且不泄露 userinfo。"""
    assert _redact_proxy_url("socks5://user:pass@host") == "socks5://host:1080"
    assert _redact_proxy_url("http://user:pass@host") == "http://host:8080"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("socks5://user:pass@127.0.0.1:7891", True),
        ("socks5://user@127.0.0.1:7891", True),
        ("socks5://127.0.0.1:7891", False),
        # 无协议 + userinfo（P1-1：urlparse 不识别，回退 @ 检测）
        ("user@127.0.0.1:7891", True),
        ("user:pass@127.0.0.1:7891", True),
        ("127.0.0.1:7891", False),
    ],
)
def test_proxy_has_auth(raw: str, expected: bool) -> None:
    assert _proxy_has_auth(raw) is expected
