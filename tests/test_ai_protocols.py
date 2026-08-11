"""AI 多协议 adapter 与 provider 集成测试。

覆盖三种协议（OpenAI Chat / OpenAI Responses / Anthropic Messages）的请求构造、
响应解析、终止状态（refusal/token 截断）处理，以及跨协议主备回退。使用
``httpx.MockTransport`` 驱动真实 ``AsyncClient``，在 handler 中断言 method、URL、
认证 header 和 JSON body，不引入额外 mock 依赖。
"""

import json
from datetime import datetime
from typing import Any, cast

import httpx
import pytest

from src.ml.ai_contracts import TEXT_RESULT_SCHEMA, VISION_RESULT_SCHEMA
from src.ml.ai_detector import (
    AIServiceConfig,
    AIServiceProvider,
    HybridAIDetector,
    PrimaryAIServiceProvider,
)
from src.ml.ai_protocols import (
    AnthropicMessagesAdapter,
    AnthropicOutputMode,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    ResponseTerminatedError,
    ResponseTermination,
    StructuredOutputMode,
    create_protocol_adapter,
)

pytestmark = pytest.mark.unit

_TEXT_RESULT = {"is_spam": True, "confidence": 0.91, "reason": "ad"}
_VISION_RESULT = {
    "is_spam": True,
    "confidence": 0.92,
    "reason": "poster",
    "extracted_text": "promo",
}
_PNG_B64 = "aW1hZ2U="  # "image" 的 base64


def _config(
    *,
    protocol: str,
    api_base: str,
    model: str = "test-model",
    structured_output_mode: str = "strict",
    anthropic_output_mode: str = "auto",
    max_retries: int = 0,
) -> AIServiceConfig:
    """构造测试用 provider 配置（默认 strict，便于断言 schema wrapper）。"""
    return AIServiceConfig(
        enabled=True,
        api_key="test-key",
        api_base=api_base,
        model=model,
        protocol=protocol,
        structured_output_mode=structured_output_mode,
        anthropic_output_mode=anthropic_output_mode,
        timeout=5,
        max_retries=max_retries,
        max_output_tokens=512,
    )


def _provider(name: str, config: AIServiceConfig) -> PrimaryAIServiceProvider:
    """绕过 PrimaryAIServiceProvider.__init__（避免读全局 settings），直接调基类构造。

    这样 provider 持有指定 config 对应的 adapter，可任意命名（primary/backup/vision_*）。
    """
    provider = PrimaryAIServiceProvider.__new__(PrimaryAIServiceProvider)
    AIServiceProvider.__init__(provider, name, config)
    return provider


def _attach_mock_transport(provider: PrimaryAIServiceProvider, handler) -> None:
    """给 provider 挂载 MockTransport 驱动的真实 AsyncClient，跳过生命周期重建检查。"""
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    now = datetime.now()
    provider._client_created_at = now
    provider._client_last_used_at = now


def _request_json(request: httpx.Request) -> dict[str, Any]:
    parsed: Any = json.loads(request.content.decode("utf-8"))
    assert isinstance(parsed, dict)
    return cast("dict[str, Any]", parsed)


# ===== 三协议请求构造（文本 + Vision）=====


async def test_openai_chat_text_and_vision_request_construction() -> None:
    """OpenAI Chat：文本无 response_format（strict 才发），Vision 用 image_url:{url, detail}。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        body = _request_json(request)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True

        user_content = body["messages"][1]["content"]
        result = _TEXT_RESULT
        if isinstance(user_content, list):
            assert user_content[0]["type"] == "text"
            image = user_content[1]
            assert image["type"] == "image_url"
            assert image["image_url"] == {
                "url": f"data:image/png;base64,{_PNG_B64}",
                "detail": "low",
            }
            result = _VISION_RESULT
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(result), "refusal": None},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = _provider(
        "chat",
        _config(
            protocol="openai_chat",
            api_base="https://api.openai.com/v1",
            structured_output_mode="strict",
        ),
    )
    _attach_mock_transport(provider, handler)
    try:
        assert await provider._call_api("hello") == _TEXT_RESULT
        assert (
            await provider._call_api_vision("system", "inspect", _PNG_B64, "image/png")
            == _VISION_RESULT
        )
    finally:
        await provider.close()
    assert calls == 2


async def test_openai_responses_text_and_vision_request_construction() -> None:
    """OpenAI Responses：input 数组 + text.format + input_image（image_url 为字符串、detail 同级）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer test-key"
        body = _request_json(request)
        assert body["max_output_tokens"] == 512
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True

        user_content = body["input"][1]["content"]
        result = _TEXT_RESULT
        if isinstance(user_content, list):
            assert user_content[0] == {"type": "input_text", "text": "inspect"}
            assert user_content[1] == {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{_PNG_B64}",
                "detail": "low",
            }
            result = _VISION_RESULT
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    # 前置 reasoning item 必须被跳过
                    {"type": "reasoning", "content": []},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(result)}],
                    },
                ],
            },
        )

    provider = _provider(
        "responses",
        _config(protocol="openai_responses", api_base="https://api.openai.com/v1"),
    )
    _attach_mock_transport(provider, handler)
    try:
        assert await provider._call_api("hello") == _TEXT_RESULT
        assert (
            await provider._call_api_vision("system", "inspect", _PNG_B64, "image/png")
            == _VISION_RESULT
        )
    finally:
        await provider.close()


async def test_anthropic_native_text_and_vision_request_construction() -> None:
    """Anthropic native：system 顶级 + max_tokens + output_config.format + source.base64。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        # GA 后不应发送 beta header
        assert "anthropic-beta" not in request.headers
        body = _request_json(request)
        assert body["system"]
        assert body["max_tokens"] == 512
        # messages 中无 system role（system 是顶级字段）
        assert all(message["role"] != "system" for message in body["messages"])

        content = body["messages"][0]["content"]
        is_vision = isinstance(content, list)
        assert body["output_config"]["format"] == {
            "type": "json_schema",
            "schema": VISION_RESULT_SCHEMA if is_vision else TEXT_RESULT_SCHEMA,
        }

        result = _TEXT_RESULT
        if is_vision:
            assert content[0] == {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _PNG_B64,
                },
            }
            # Anthropic 无 detail 概念
            assert "detail" not in content[0]
            result = _VISION_RESULT
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(result)}],
                "stop_reason": "end_turn",
            },
        )

    provider = _provider(
        "anthropic_native",
        _config(
            protocol="anthropic_messages",
            api_base="https://api.anthropic.com",
            model="claude-sonnet-4-5-20250929",
            anthropic_output_mode="auto",  # 命中 allowlist → native
        ),
    )
    _attach_mock_transport(provider, handler)
    try:
        assert await provider._call_api("hello") == _TEXT_RESULT
        assert (
            await provider._call_api_vision("system", "inspect", _PNG_B64, "image/png")
            == _VISION_RESULT
        )
    finally:
        await provider.close()


async def test_anthropic_tool_request_and_response() -> None:
    """Anthropic tool 模式：tools + tool_choice 强制，响应从 tool_use.input 取 dict。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = _request_json(request)
        assert "output_config" not in body
        assert body["tools"][0]["input_schema"] == TEXT_RESULT_SCHEMA
        assert body["tools"][0]["strict"] is True
        assert body["tool_choice"] == {
            "type": "tool",
            "name": "report_spam_detection",
            "disable_parallel_tool_use": True,
        }
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "calling tool"},
                    {
                        "type": "tool_use",
                        "name": "report_spam_detection",
                        "input": _TEXT_RESULT,
                    },
                ],
                "stop_reason": "tool_use",
            },
        )

    provider = _provider(
        "anthropic_tool",
        _config(
            protocol="anthropic_messages",
            api_base="https://api.anthropic.com/v1",
            model="claude-3-5-sonnet",  # 不在 native allowlist → auto 走 tool
            anthropic_output_mode="auto",
        ),
    )
    assert isinstance(provider.adapter, AnthropicMessagesAdapter)
    assert provider.adapter.output_mode is AnthropicOutputMode.TOOL
    _attach_mock_transport(provider, handler)
    try:
        assert await provider._call_api("hello") == _TEXT_RESULT
    finally:
        await provider.close()


async def test_openai_legacy_omits_schema_and_uses_json_object_for_vision() -> None:
    """legacy 模式：文本不发 response_format，Vision 发 json_object，依赖 JSON 兜底解析。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = _request_json(request)
        user_content = body["messages"][1]["content"]
        if isinstance(user_content, list):
            assert body["response_format"] == {"type": "json_object"}
            result = _VISION_RESULT
        else:
            assert "response_format" not in body
            result = _TEXT_RESULT
        # legacy 模拟模型在 JSON 外附加说明文本，验证兜底解析
        decorated = f"result follows\n{json.dumps(result)}\nend"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": decorated, "refusal": None},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = _provider(
        "legacy",
        _config(
            protocol="openai_chat",
            api_base="https://compatible.example/v1",  # openai_chat + auto → legacy
            structured_output_mode="auto",
        ),
    )
    assert isinstance(provider.adapter, OpenAIChatAdapter)
    assert provider.adapter.structured_output_mode is StructuredOutputMode.LEGACY
    _attach_mock_transport(provider, handler)
    try:
        assert await provider._call_api("hello") == _TEXT_RESULT
        assert (
            await provider._call_api_vision("system", "inspect", _PNG_B64, "image/png")
            == _VISION_RESULT
        )
    finally:
        await provider.close()
    assert calls == 2


# ===== Anthropic URL /v1 双形式 =====


@pytest.mark.parametrize(
    ("api_base", "expected"),
    [
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("https://gateway.example/anthropic", "https://gateway.example/anthropic/v1/messages"),
        ("https://gateway.example/anthropic/v1/", "https://gateway.example/anthropic/v1/messages"),
    ],
)
def test_anthropic_url_accepts_versioned_and_unversioned_base(api_base: str, expected: str) -> None:
    adapter = create_protocol_adapter(
        protocol="anthropic_messages",
        model="claude-sonnet-4-5-20250929",
        structured_output_mode="strict",
        anthropic_output_mode="native",
    )
    assert adapter.build_url(api_base) == expected


# ===== 终止响应解析（refusal / max_tokens / content_filter）=====


@pytest.mark.parametrize(
    ("response_json", "termination"),
    [
        (
            {
                "choices": [
                    {
                        "message": {"content": None, "refusal": "blocked"},
                        "finish_reason": "stop",
                    }
                ]
            },
            ResponseTermination.REFUSAL,
        ),
        (
            {
                "choices": [
                    {
                        "message": {"content": "{", "refusal": None},
                        "finish_reason": "length",
                    }
                ]
            },
            ResponseTermination.MAX_TOKENS,
        ),
        (
            {
                "choices": [
                    {
                        "message": {"content": "{", "refusal": None},
                        "finish_reason": "content_filter",
                    }
                ]
            },
            ResponseTermination.CONTENT_FILTER,
        ),
    ],
)
def test_chat_terminal_response_parsing(
    response_json: dict[str, Any], termination: ResponseTermination
) -> None:
    adapter = OpenAIChatAdapter(StructuredOutputMode.STRICT)
    assert adapter.parse_response(response_json).termination is termination


def test_responses_parser_skips_reasoning_item() -> None:
    """Responses 响应中 reasoning item 在 message 之前，必须跳过。"""
    adapter = OpenAIResponsesAdapter(StructuredOutputMode.STRICT)
    parsed = adapter.parse_response(
        {
            "status": "completed",
            "output": [
                {"type": "reasoning", "content": []},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(_TEXT_RESULT)}],
                },
            ],
        }
    )
    assert parsed.result == _TEXT_RESULT


@pytest.mark.parametrize(
    ("response_json", "termination"),
    [
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "blocked"}],
                    }
                ],
            },
            ResponseTermination.REFUSAL,
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
            ResponseTermination.MAX_TOKENS,
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": [],
            },
            ResponseTermination.CONTENT_FILTER,
        ),
    ],
)
def test_responses_terminal_response_parsing(
    response_json: dict[str, Any], termination: ResponseTermination
) -> None:
    adapter = OpenAIResponsesAdapter(StructuredOutputMode.STRICT)
    assert adapter.parse_response(response_json).termination is termination


@pytest.mark.parametrize(
    ("response_json", "termination"),
    [
        (
            {"content": [{"type": "text", "text": "blocked"}], "stop_reason": "refusal"},
            ResponseTermination.REFUSAL,
        ),
        (
            {"content": [{"type": "text", "text": "{"}], "stop_reason": "max_tokens"},
            ResponseTermination.MAX_TOKENS,
        ),
        (
            {
                "content": [{"type": "text", "text": "{"}],
                "stop_reason": "model_context_window_exceeded",
            },
            ResponseTermination.MAX_TOKENS,
        ),
    ],
)
def test_anthropic_terminal_response_parsing(
    response_json: dict[str, Any], termination: ResponseTermination
) -> None:
    adapter = AnthropicMessagesAdapter(StructuredOutputMode.STRICT, AnthropicOutputMode.NATIVE)
    assert adapter.parse_response(response_json).termination is termination


# ===== 协调器：跨协议回退、终止不重试不计熔断 =====


async def test_refusal_skips_retry_and_falls_back_across_protocols() -> None:
    """主 Chat refusal 不重试、不计熔断，直接走备 Anthropic tool 成功。"""
    primary_calls = 0
    backup_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "refusal": "blocked"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    def backup_handler(request: httpx.Request) -> httpx.Response:
        nonlocal backup_calls
        backup_calls += 1
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "report_spam_detection",
                        "input": _TEXT_RESULT,
                    }
                ],
                "stop_reason": "tool_use",
            },
        )

    primary = _provider(
        "primary",
        _config(
            protocol="openai_chat",
            api_base="https://api.openai.com/v1",
            max_retries=3,
        ),
    )
    backup = _provider(
        "backup",
        _config(
            protocol="anthropic_messages",
            api_base="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            anthropic_output_mode="tool",
            max_retries=3,
        ),
    )
    _attach_mock_transport(primary, primary_handler)
    _attach_mock_transport(backup, backup_handler)

    detector = HybridAIDetector()
    detector.primary = primary
    detector.backup = backup
    try:
        result = await detector.detect("spam")
    finally:
        await primary.close()
        await backup.close()

    assert result["details"]["provider"] == "backup"
    assert primary_calls == 1  # refusal 不重试
    assert backup_calls == 1
    # 终止类错误不计入熔断统计
    assert detector._stats["primary"].failure_count == 0
    assert detector._stats["primary"].consecutive_failures == 0


async def test_vision_coordinator_falls_back_on_termination() -> None:
    """Vision 主 refusal 不重试，协调器直接走 Vision 备成功。"""
    primary_calls = 0
    backup_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "refusal": "blocked"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    def backup_handler(request: httpx.Request) -> httpx.Response:
        nonlocal backup_calls
        backup_calls += 1
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(_VISION_RESULT)}],
                "stop_reason": "end_turn",
            },
        )

    primary = _provider(
        "vision_primary",
        _config(protocol="openai_chat", api_base="https://api.openai.com/v1"),
    )
    backup = _provider(
        "vision_backup",
        _config(
            protocol="anthropic_messages",
            api_base="https://api.anthropic.com",
            model="claude-sonnet-4-5-20250929",
            anthropic_output_mode="native",
        ),
    )
    _attach_mock_transport(primary, primary_handler)
    _attach_mock_transport(backup, backup_handler)

    detector = HybridAIDetector()
    detector.vision_primary = primary
    detector.vision_backup = backup
    try:
        result = await detector.detect_image_with_context(_PNG_B64, "image/png")
    finally:
        await primary.close()
        await backup.close()

    assert result["details"]["provider"] == "vision_backup"
    assert primary_calls == 1
    assert backup_calls == 1
    assert detector._stats["vision_primary"].failure_count == 0


async def test_provider_surfaces_terminal_response_without_retry() -> None:
    """provider.detect 在终止类响应时不重试，直接抛 ResponseTerminatedError。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "{", "refusal": None},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    provider = _provider(
        "primary",
        _config(
            protocol="openai_chat",
            api_base="https://api.openai.com/v1",
            max_retries=2,
        ),
    )
    _attach_mock_transport(provider, handler)
    try:
        with pytest.raises(ResponseTerminatedError) as exc_info:
            await provider.detect("spam")
    finally:
        await provider.close()
    assert calls == 1  # max_tokens 截断不重试
    assert exc_info.value.termination is ResponseTermination.MAX_TOKENS


async def test_detect_with_context_falls_back_on_termination() -> None:
    """带上下文检测：主 refusal 不重试不计熔断，直接走备份（与 detect 对称）。"""
    primary_calls = 0
    backup_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "refusal": "blocked"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    def backup_handler(request: httpx.Request) -> httpx.Response:
        nonlocal backup_calls
        backup_calls += 1
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "report_spam_detection",
                        "input": _TEXT_RESULT,
                    }
                ],
                "stop_reason": "tool_use",
            },
        )

    primary = _provider(
        "primary",
        _config(
            protocol="openai_chat",
            api_base="https://api.openai.com/v1",
            max_retries=3,
        ),
    )
    backup = _provider(
        "backup",
        _config(
            protocol="anthropic_messages",
            api_base="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            anthropic_output_mode="tool",
            max_retries=3,
        ),
    )
    _attach_mock_transport(primary, primary_handler)
    _attach_mock_transport(backup, backup_handler)

    detector = HybridAIDetector()
    detector.primary = primary
    detector.backup = backup
    try:
        result = await detector.detect_with_context("spam", "some context")
    finally:
        await primary.close()
        await backup.close()

    assert result["details"]["provider"] == "backup"
    assert primary_calls == 1
    assert backup_calls == 1
    assert detector._stats["primary"].failure_count == 0
