"""httpx 异常格式化工具

提供统一的 httpx 异常诊断摘要，解决超时/网络异常 ``str(e)`` 为空导致日志丢失
失败原因的问题（CAS 与 AI 检测器共用的可观测性基础设施）。

设计要点：
- 始终输出异常类名，空消息不再丢失类型信息
- 超时异常细分为 connect/read/write/pool 阶段，并附带有效超时秒数
- HTTPStatusError 提取状态码；响应体支持三种策略（安全提取/原文截断/不输出）
- 安全模式下对 URL 脱敏，避免查询参数中的密钥泄露到日志
"""

import contextlib
import json
import re
from typing import Literal

import httpx

ResponseBodyMode = Literal["safe", "raw", "none"]
ErrorTypeMode = Literal["field", "name"]

# 匹配消息中的完整 URL，安全模式下替换为 <url>，防止查询参数泄露密钥
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")

# 超时异常子类到阶段的映射（顺序敏感：具体子类在前）
_TIMEOUT_PHASES: tuple[tuple[type[httpx.TimeoutException], str], ...] = (
    (httpx.ConnectTimeout, "connect"),
    (httpx.ReadTimeout, "read"),
    (httpx.WriteTimeout, "write"),
    (httpx.PoolTimeout, "pool"),
)

# 安全模式下允许从响应体 error 对象中提取的字段白名单
_SAFE_ERROR_KEYS: tuple[str, ...] = ("code", "type", "message")


def _truncate(message: str, limit: int = 200) -> str:
    """压缩空白并截断超长消息"""
    normalized = re.sub(r"\s+", " ", message).strip()
    if len(normalized) > limit:
        return normalized[: limit - 3] + "..."
    return normalized


def _redact_urls(message: str) -> str:
    """将消息中的 URL 替换为 <url>，防止查询参数中的密钥泄露"""
    return _URL_PATTERN.sub("<url>", message)


def _sanitize_value(value: object) -> str | int | float | bool | None:
    """仅保留标量值，字符串做 URL 脱敏，其余类型丢弃"""
    if isinstance(value, str):
        return _redact_urls(value)
    if isinstance(value, (int, float, bool)):
        return value
    return None


def _safe_response_body(response: httpx.Response) -> str:
    """从响应体安全提取错误摘要

    仅解析 JSON 并提取白名单字段（``error.code/type/message`` 或顶层 ``description``），
    其余内容一律丢弃；提取后做 URL 脱敏与长度截断。解析失败或无可用字段时返回空串。
    """
    try:
        data = response.json()
    except Exception:
        return ""

    if not isinstance(data, dict):
        return ""

    safe: dict[str, object] = {}

    error_value = data.get("error")
    if isinstance(error_value, dict):
        safe_error: dict[str, str | int | float | bool] = {}
        for key in _SAFE_ERROR_KEYS:
            sanitized = _sanitize_value(error_value.get(key))
            if sanitized is not None:
                safe_error[key] = sanitized
        if safe_error:
            safe["error"] = safe_error
    else:
        sanitized = _sanitize_value(error_value)
        if sanitized is not None:
            safe["error"] = sanitized

    description = _sanitize_value(data.get("description"))
    if description is not None:
        safe["description"] = description

    if not safe:
        return ""

    return _truncate(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))


def _timeout_phase(error: httpx.TimeoutException) -> str | None:
    """根据异常子类判定超时阶段"""
    for error_type, phase in _TIMEOUT_PHASES:
        if isinstance(error, error_type):
            return phase
    return None


def _timeout_seconds(
    timeout: httpx.Timeout | int | float | None, phase: str | None
) -> int | float | None:
    """从超时配置中取对应阶段的有效秒数

    超时异常本身不携带配置值，需由调用方传入实际请求使用的 ``httpx.Timeout``
    （通常是 ``client.timeout``）或统一的秒数。
    """
    if phase is None or timeout is None:
        return None
    if isinstance(timeout, httpx.Timeout):
        return getattr(timeout, phase, None)
    return timeout


def _format_message(error: BaseException, *, redact: bool) -> str:
    """提取异常消息，按需做 URL 脱敏与截断"""
    message = str(error)
    if redact:
        message = _redact_urls(message)
    return _truncate(message)


def _append_cause(parts: list[str], error: BaseException, *, redact: bool) -> None:
    """安全追加 ``__cause__`` 摘要，便于区分 DNS/拒绝连接/TLS 等底层错误"""
    cause = error.__cause__
    if cause is None:
        return
    cause_type = cause.__class__.__name__
    cause_message = _format_message(cause, redact=redact)
    summary = f"{cause_type}: {cause_message}" if cause_message else cause_type
    parts.append(f"[cause={summary}]")


def format_httpx_error(
    error: httpx.HTTPError,
    *,
    response_body_mode: ResponseBodyMode = "safe",
    timeout: httpx.Timeout | int | float | None = None,
    include_cause: bool = False,
    error_type_mode: ErrorTypeMode = "field",
) -> str:
    """格式化 httpx 异常为稳定的诊断摘要

    Args:
        error: httpx 抛出的异常（TimeoutException / HTTPStatusError / RequestError 等）
        response_body_mode: 响应体处理策略
            - ``"safe"``：解析 JSON 仅提取白名单字段并脱敏（默认，推荐新接入方使用）
            - ``"raw"``：截断响应原文（AI 检测器历史行为，保持兼容）
            - ``"none"``：不输出响应体
        timeout: 实际请求使用的超时配置，用于补充超时阶段的有效秒数
        include_cause: 是否追加 ``__cause__`` 摘要（底层网络错误诊断）
        error_type_mode: 异常类型的呈现方式
            - ``"field"``：``[error_type=ReadTimeout]``（默认，结构化）
            - ``"name"``：``ReadTimeout``（AI 检测器历史格式，保持兼容）

    Returns:
        形如 ``[error_type=ReadTimeout] [phase=read] [timeout_seconds=5]`` 的摘要；
        始终包含异常类名，空消息不会丢失类型信息。
    """
    error_type = error.__class__.__name__
    type_part = f"[error_type={error_type}]" if error_type_mode == "field" else error_type
    redact = response_body_mode != "raw"
    parts: list[str] = [type_part]

    # 超时异常：补充阶段与有效秒数
    if isinstance(error, httpx.TimeoutException):
        phase = _timeout_phase(error)
        if phase is not None:
            parts.append(f"[phase={phase}]")
        seconds = _timeout_seconds(timeout, phase)
        if seconds is not None:
            parts.append(f"[timeout_seconds={seconds}]")
        message = _format_message(error, redact=redact)
        if message:
            parts.append(f"[message={message}]")
        if include_cause:
            _append_cause(parts, error, redact=redact)
        return " ".join(parts)

    # HTTP 状态错误：补充状态码与响应体
    if isinstance(error, httpx.HTTPStatusError):
        parts.append(f"[status_code={error.response.status_code}]")

        response_body = ""
        if response_body_mode == "raw":
            with contextlib.suppress(Exception):
                response_body = _truncate(error.response.text)
        elif response_body_mode == "safe":
            response_body = _safe_response_body(error.response)

        if response_body:
            parts.append(f"[response={response_body}]")

        # raw 模式保留 AI 历史输出（含 message，可能带 URL）；
        # safe/none 不追加 message，因为 HTTPStatusError.__str__ 含完整 URL
        if response_body_mode == "raw":
            message = _format_message(error, redact=False)
            if message:
                parts.append(f"[message={message}]")
        return " ".join(parts)

    # 其他请求错误（ConnectError / ProxyError / RemoteProtocolError 等）
    message = _format_message(error, redact=redact)
    if message:
        parts.append(f"[message={message}]")
    if include_cause and isinstance(error, httpx.RequestError):
        _append_cause(parts, error, redact=redact)
    return " ".join(parts)
