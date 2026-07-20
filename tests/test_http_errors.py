"""httpx 异常格式化工具测试"""

import httpx
import pytest

from src.core.http_errors import format_httpx_error

_TIMEOUT = httpx.Timeout(5, connect=3.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_type", "phase", "timeout_seconds"),
    [
        (httpx.ConnectTimeout, "connect", "3.0"),
        (httpx.ReadTimeout, "read", "5"),
        (httpx.WriteTimeout, "write", "5"),
        (httpx.PoolTimeout, "pool", "5"),
    ],
)
def test_empty_timeout_includes_type_phase_and_effective_timeout(
    error_type, phase, timeout_seconds
):
    """空消息超时仍应记录异常类型、阶段与对应阶段的有效超时值"""
    error = error_type("")

    formatted = format_httpx_error(error, timeout=_TIMEOUT)

    assert formatted == (
        f"[error_type={error_type.__name__}] "
        f"[phase={phase}] [timeout_seconds={timeout_seconds}]"
    )


@pytest.mark.unit
def test_empty_connect_error_falls_back_to_type_name():
    """空消息的网络异常至少保留异常类名"""
    assert format_httpx_error(httpx.ConnectError("")) == "[error_type=ConnectError]"


@pytest.mark.unit
def test_request_error_message_is_redacted_in_safe_mode():
    """safe 模式下消息中的 URL 应被脱敏"""
    error = httpx.ConnectError("failed to connect to https://secret.example.test/path")

    formatted = format_httpx_error(error)

    assert "[error_type=ConnectError]" in formatted
    assert "<url>" in formatted
    assert "secret.example.test" not in formatted


@pytest.mark.unit
def test_request_error_can_include_sanitized_cause():
    """带 __cause__ 时应输出底层错误摘要，且对 URL 脱敏"""
    error = httpx.ConnectError("")
    error.__cause__ = OSError("connection refused for https://secret.example.test/path")

    formatted = format_httpx_error(error, include_cause=True)

    assert "[cause=OSError: connection refused for <url>]" in formatted
    assert "secret.example.test" not in formatted


@pytest.mark.unit
def test_http_status_safe_mode_extracts_allowlisted_json_fields():
    """safe 模式仅提取 error.code/type/message 与 description，丢弃其余字段"""
    request = httpx.Request("GET", "https://api.example.test/check?token=do-not-log")
    response = httpx.Response(
        503,
        json={
            "error": {
                "code": "overloaded",
                "type": "server_error",
                "message": "See https://status.example.test/incidents/1",
                "internal": "do-not-log",
            },
            "debug": "do-not-log",
        },
        request=request,
    )
    error = httpx.HTTPStatusError(
        "503 for https://api.example.test/check?token=do-not-log",
        request=request,
        response=response,
    )

    formatted = format_httpx_error(error)

    assert "[error_type=HTTPStatusError]" in formatted
    assert "[status_code=503]" in formatted
    assert '"code":"overloaded"' in formatted
    assert '"type":"server_error"' in formatted
    assert '"message":"See <url>"' in formatted
    # 白名单外的字段与敏感内容不得出现在日志中
    assert "internal" not in formatted
    assert "debug" not in formatted
    assert "do-not-log" not in formatted
    assert "api.example.test" not in formatted


@pytest.mark.unit
def test_http_status_safe_mode_extracts_description():
    """顶层 description 字段也应被安全提取"""
    request = httpx.Request("GET", "https://api.example.test/check")
    response = httpx.Response(
        429,
        json={"description": "Rate limited", "details": "do-not-log"},
        request=request,
    )
    error = httpx.HTTPStatusError(
        "429 for https://api.example.test/check",
        request=request,
        response=response,
    )

    formatted = format_httpx_error(error)

    assert '[response={"description":"Rate limited"}]' in formatted
    assert "details" not in formatted


@pytest.mark.unit
def test_http_status_raw_name_mode_preserves_ai_format():
    """raw + name 模式保持 AI 检测器历史输出格式（含响应原文与 message）"""
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(
        429,
        text='{"error":{"message":"rate limited"}}',
        request=request,
    )
    error = httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)

    formatted = format_httpx_error(error, response_body_mode="raw", error_type_mode="name")

    assert formatted == (
        "HTTPStatusError [status_code=429] "
        '[response={"error":{"message":"rate limited"}}] '
        "[message=429 Too Many Requests]"
    )
