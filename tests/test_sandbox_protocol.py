"""沙盒协议模型与严格 JSON 校验的单元测试。"""

import pytest
from pydantic import ValidationError

from sandbox.protocol import (
    MAX_JSON_DEPTH,
    PROTOCOL_VERSION,
    AskResult,
    ExecuteRequest,
    validate_json_value,
)


def _valid_request(**overrides):
    base = {
        "protocol_version": PROTOCOL_VERSION,
        "language": "python",
        "source": "def ask(ctx):\n    return {'text': 'hi'}\n",
        "entry": "ask",
        "context": {"user": {"id": 1}},
        "timeout_ms": 2000,
    }
    base.update(overrides)
    return base


class TestValidateJsonValue:
    def test_accepts_scalars_and_nested_structures(self):
        validate_json_value({"a": [1, 2.5, True, None, {"b": "c"}]})

    def test_rejects_nan_and_infinity(self):
        with pytest.raises(ValueError, match=r"NaN|Infinity"):
            validate_json_value(float("nan"))
        with pytest.raises(ValueError):
            validate_json_value({"x": float("inf")})

    @pytest.mark.parametrize("bad", [object(), {1: "x"}, (1, 2), bytearray(b"y")])
    def test_rejects_non_json_types(self, bad):
        with pytest.raises(ValueError):
            validate_json_value(bad)

    def test_rejects_depth_beyond_limit(self):
        deep: object = None
        for _ in range(MAX_JSON_DEPTH + 5):
            deep = {"n": deep}
        with pytest.raises(ValueError, match="深度"):
            validate_json_value(deep)


class TestAskResult:
    def test_accepts_minimal_text_only(self):
        result = AskResult.model_validate({"text": "1+1=?"})
        assert result.options == []
        assert result.state is None

    def test_accepts_full_payload(self):
        result = AskResult.model_validate(
            {
                "text": "选一个",
                "options": [{"text": "甲", "value": "opt_a"}, {"text": "乙", "value": "opt_b"}],
                "state": {"answer": "opt_a"},
            }
        )
        assert len(result.options) == 2

    def test_rejects_empty_text(self):
        with pytest.raises(ValidationError):
            AskResult.model_validate({"text": ""})

    def test_rejects_overlong_text(self):
        with pytest.raises(ValidationError):
            AskResult.model_validate({"text": "题" * 2001})

    def test_rejects_too_many_options(self):
        options = [{"text": f"选项{i}", "value": f"v{i}"} for i in range(9)]
        with pytest.raises(ValidationError):
            AskResult.model_validate({"text": "题", "options": options})

    def test_rejects_bad_option_value_charset(self):
        with pytest.raises(ValidationError):
            AskResult.model_validate(
                {"text": "题", "options": [{"text": "a", "value": "bad value!"}]}
            )

    def test_rejects_oversized_state(self):
        with pytest.raises(ValidationError):
            AskResult.model_validate({"text": "题", "state": {"blob": "x" * 5000}})

    def test_rejects_nul_byte_in_text(self):
        with pytest.raises(ValidationError):
            AskResult.model_validate({"text": "坏\x00题"})


class TestExecuteRequest:
    def test_accepts_valid_request(self):
        request = ExecuteRequest.model_validate(_valid_request())
        assert request.entry == "ask"

    def test_rejects_wrong_protocol_version(self):
        with pytest.raises(ValidationError, match="协议版本"):
            ExecuteRequest.model_validate(_valid_request(protocol_version=999))

    def test_rejects_unknown_language(self):
        with pytest.raises(ValidationError):
            ExecuteRequest.model_validate(_valid_request(language="ruby"))

    def test_rejects_unknown_entry(self):
        with pytest.raises(ValidationError):
            ExecuteRequest.model_validate(_valid_request(entry="ban"))

    def test_rejects_empty_source(self):
        with pytest.raises(ValidationError):
            ExecuteRequest.model_validate(_valid_request(source="   "))

    def test_rejects_timeout_out_of_range(self):
        with pytest.raises(ValidationError):
            ExecuteRequest.model_validate(_valid_request(timeout_ms=99999))

    def test_rejects_deep_context(self):
        deep = None
        for _ in range(MAX_JSON_DEPTH + 5):
            deep = {"n": deep}
        with pytest.raises(ValidationError):
            ExecuteRequest.model_validate(_valid_request(context=deep))
