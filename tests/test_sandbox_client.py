"""SandboxClient 的异常归类与响应边界测试（httpx MockTransport，无真实网络）。

归错语义与失败处理直接挂钩（降级 vs 不计答错），这里锁死：
- 连接失败/超时/5xx/429 → SandboxUnavailableError（服务降级）
- 4xx/响应不合规/超大响应 → SandboxProtocolError（执行故障，不罚用户）
"""

import json
import time

import httpx
import pytest

from src.core.config import settings
from src.services.sandbox_client import (
    MAX_RESPONSE_BYTES,
    SandboxClient,
    SandboxProtocolError,
    SandboxUnavailableError,
)

pytestmark = [pytest.mark.unit]

_API_URL = "http://sandbox.test"
_KEY = "k" * 32


@pytest.fixture(autouse=True)
def _sandbox_settings(monkeypatch):
    """测试环境默认无沙盒配置，统一注入已配置状态。"""
    monkeypatch.setattr(settings, "sandbox_api_url", _API_URL)
    monkeypatch.setattr(settings, "sandbox_api_key", _KEY)


def _client_with(handler) -> SandboxClient:
    """构造带 mock 传输与预置健康态的客户端（跳过真实健康探测）。"""
    client = SandboxClient()
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(transport=transport)
    # 预置健康缓存，绕过 /healthz 往返
    client._healthy_until = time.monotonic() + 3600
    return client


def _ok_response(result: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "result": result, "error": None, "detail": ""},
        request=httpx.Request("POST", f"{_API_URL}/execute"),
    )


_ASK_RESULT = {"text": "题面", "options": [], "state": None}


class TestErrorClassification:
    async def test_connection_error_is_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        client = _client_with(handler)
        with pytest.raises(SandboxUnavailableError):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_5xx_is_unavailable(self):
        def handler(request):
            return httpx.Response(500, text="boom", request=request)

        client = _client_with(handler)
        with pytest.raises(SandboxUnavailableError):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_400_is_protocol_error(self):
        def handler(request):
            return httpx.Response(400, text="bad request", request=request)

        client = _client_with(handler)
        with pytest.raises(SandboxProtocolError):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_busy_payload_is_unavailable(self):
        def handler(request):
            return httpx.Response(
                200,
                json={"ok": False, "result": None, "error": "busy", "detail": "满"},
                request=request,
            )

        client = _client_with(handler)
        with pytest.raises(SandboxUnavailableError):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_script_crash_payload_is_protocol_error(self):
        def handler(request):
            return httpx.Response(
                200,
                json={"ok": False, "result": None, "error": "crash", "detail": "X"},
                request=request,
            )

        client = _client_with(handler)
        with pytest.raises(SandboxProtocolError):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_success_returns_typed_result(self):
        def handler(request):
            return _ok_response(_ASK_RESULT)

        client = _client_with(handler)
        result = await client.execute_ask("python", "def ask(ctx): ...", {})
        assert result.text == "题面"


class TestResponseBoundaries:
    async def test_oversized_response_rejected(self):
        def handler(request):
            # 超大合法 JSON：读取阶段就应被截断拒绝
            return httpx.Response(
                200,
                content=b"x" * (MAX_RESPONSE_BYTES + 1024),
                request=request,
            )

        client = _client_with(handler)
        with pytest.raises(SandboxProtocolError, match="大小限制"):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_non_json_response_rejected(self):
        def handler(request):
            return httpx.Response(200, content=b"not json", request=request)

        client = _client_with(handler)
        with pytest.raises(SandboxProtocolError, match="JSON"):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_loose_typed_ok_field_rejected(self):
        # pydantic strict 模式：字符串 "true" 不得被强转为布尔
        def handler(request):
            return httpx.Response(
                200,
                content=json.dumps({"ok": "true", "result": _ASK_RESULT}),
                request=request,
            )

        client = _client_with(handler)
        with pytest.raises(SandboxProtocolError, match="不合规"):
            await client.execute("python", "def ask(ctx): ...", "ask", {})

    async def test_unknown_field_rejected(self):
        def handler(request):
            body = {"ok": True, "result": _ASK_RESULT, "extra_field": 1}
            return httpx.Response(200, content=json.dumps(body), request=request)

        client = _client_with(handler)
        with pytest.raises(SandboxProtocolError, match="不合规"):
            await client.execute("python", "def ask(ctx): ...", "ask", {})


class TestNotConfigured:
    async def test_unconfigured_raises_unavailable_without_http(self, monkeypatch):
        monkeypatch.setattr(settings, "sandbox_api_url", "")
        monkeypatch.setattr(settings, "sandbox_api_key", "")
        client = SandboxClient()  # 无传输注入——任何 HTTP 尝试都会失败
        with pytest.raises(SandboxUnavailableError, match="未配置"):
            await client.execute("python", "def ask(ctx): ...", "ask", {})
