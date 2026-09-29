"""沙盒控制面失败响应出口的防御性测试（detail 截断 + error 枚举归一）。"""

import json

import pytest

from sandbox.protocol import (
    MAX_DETAIL_CHARS,
    PROTOCOL_VERSION,
    ExecuteRequest,
)
from sandbox.server import _build_response

pytestmark = pytest.mark.unit


def _request() -> ExecuteRequest:
    return ExecuteRequest(
        protocol_version=PROTOCOL_VERSION,
        language="python",
        source="def ask(ctx):\n    return {'text': 'ok'}\n",
        entry="ask",
        context={},
        timeout_ms=2000,
    )


class TestBuildResponseFailureBranch:
    """失败分支出口必须闭合：任何 runner 原始结果都不能让构造抛 ValidationError。"""

    def test_truncates_overlong_detail(self) -> None:
        """runner 采集的 stderr 可达 16KB，出口必须截到协议上限而非抛校验异常。"""
        payload = _build_response(
            _request(),
            {"ok": False, "error": "timeout", "detail": "x" * (MAX_DETAIL_CHARS + 128)},
        )

        assert payload.ok is False
        assert payload.error == "timeout"
        assert payload.detail == "x" * MAX_DETAIL_CHARS

    def test_normalizes_invalid_error(self) -> None:
        """脚本经 harness stdout 可间接控制 error 字段，非枚举值归一为 bad_output。"""
        payload = _build_response(
            _request(),
            {"ok": False, "error": "attacker-controlled", "detail": "failure"},
        )

        assert payload.ok is False
        assert payload.error == "bad_output"
        assert payload.detail == "failure"

    def test_clears_non_string_detail(self) -> None:
        payload = _build_response(
            _request(),
            {"ok": False, "error": "crash", "detail": {"secret": "value"}},
        )

        assert payload.ok is False
        assert payload.error == "crash"
        assert payload.detail == ""

    def test_preserves_normal_failure(self) -> None:
        payload = _build_response(
            _request(),
            {"ok": False, "error": "memory", "detail": "MemoryError"},
        )

        assert payload.ok is False
        assert payload.result is None
        assert payload.error == "memory"
        assert payload.detail == "MemoryError"

    def test_invalid_result_error_text_is_truncated(self) -> None:
        """ok=True 但 result 结构非法：pydantic 校验错误全文（脚本可放大）必须截断。

        回归 F4 残口：错误文本超 4096 时构造 ExecuteResponse 二次抛异常 → HTTP 500。
        """
        # 大量未知字段 + 超限字段名，extra=forbid 的逐行报错足以累积超 4096 字符
        malicious_result = {f"field_{i}_{'x' * 40}": "value" for i in range(200)}

        payload = _build_response(_request(), {"ok": True, "result": malicious_result})

        assert payload.ok is False
        assert payload.error == "bad_output"
        assert len(payload.detail) <= 4096
        assert payload.detail.startswith("脚本返回结构不合规")

    def test_protocol_error_truncates_message(self) -> None:
        """_protocol_error 的 message（可含请求方控制的校验错误全文）同样截断。"""
        from sandbox.server import _protocol_error

        exc = _protocol_error("y" * 9000, status=400)

        body = json.loads(exc.text)
        assert body["error"] == "bad_input"
        assert len(body["detail"]) == 4096
