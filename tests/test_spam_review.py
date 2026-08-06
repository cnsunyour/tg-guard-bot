"""垃圾消息人工复核状态基础设施测试。

测试策略：``_FakeRedis`` 模拟 redis-py 的 ``set`` / ``get`` / ``eval`` 调用契约
（含 Lua 脚本语义），验证 Python 侧的状态机逻辑与调用方式；**不等同于真实 Redis
Lua 集成测试**，Lua 脚本在真 Redis 的行为待真机验证（3b-3 接入后）。
"""

import json
import re
from unittest.mock import patch

import pytest

from src.core.redis import RedisKeys
from src.services import spam_review
from src.services.spam_review import (
    SpamMessageType,
    SpamReviewState,
    consume_review_state,
    create_review_state,
    delete_review_state_if_match,
    get_review_state,
    review_lock,
)

pytestmark = pytest.mark.unit


def _state(
    *,
    review_id: str = "0123456789abcdef",
    original_text: str = "原始 caption",
) -> SpamReviewState:
    return SpamReviewState(
        review_id=review_id,
        offender_user_id=42,
        message_type=SpamMessageType.photo,
        original_text=original_text,
        recognized_text="OCR 识别文本",
        sample_text="原始 caption\nOCR 识别文本",
        reason_codes=("rule:url", "vision:二维码"),
        confidence=0.91,
    )


def _payload() -> dict[str, object]:
    return json.loads(_state().to_json())


class _FakeRedis:
    """用于验证 Redis 调用边界的最小内存替身（模拟 Lua 脚本语义）。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int | None] = {}

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    def _delete(self, key: str) -> int:
        if key not in self.values:
            return 0
        del self.values[key]
        self.expirations.pop(key, None)
        return 1

    async def eval(
        self,
        script: str,
        numkeys: int,
        key: str,
        argument: str,
    ) -> str | int | None:
        assert numkeys == 1

        if script == spam_review._RELEASE_REVIEW_LOCK_SCRIPT:
            if self.values.get(key) == argument:
                return self._delete(key)
            return 0

        raw = self.values.get(key)
        if raw is None:
            return None if script == spam_review._CONSUME_REVIEW_SCRIPT else 0

        current_state = SpamReviewState.from_json(raw)

        if script == spam_review._CONSUME_REVIEW_SCRIPT:
            if current_state.review_id == argument:
                self._delete(key)
            return raw

        if script == spam_review._DELETE_REVIEW_IF_MATCH_SCRIPT:
            if current_state.review_id == argument:
                return self._delete(key)
            return 0

        raise AssertionError("unexpected Lua script")


def test_spam_message_type_values() -> None:
    assert [message_type.value for message_type in SpamMessageType] == [
        "text",
        "photo",
        "sticker",
        "edited_text",
        "edited_photo",
    ]


def test_review_keys() -> None:
    assert RedisKeys.spam_review(-100, 123) == "spam_review:-100:123"
    assert RedisKeys.spam_review_lock(-100, 123) == "spam_review_lock:-100:123"


def test_to_json_from_json_roundtrip_preserves_original_text() -> None:
    original_text = '中文 <b>不转义</b> 😀 "quoted"\n反斜杠\\'
    state = SpamReviewState(
        offender_user_id=42,
        message_type=SpamMessageType.text,
        original_text=original_text,
        recognized_text=None,
        sample_text=original_text,
        reason_codes=("rule:url", "原始原因:特殊字符😀"),
        confidence=0.75,
    )

    raw = state.to_json()
    restored = SpamReviewState.from_json(raw)

    assert restored == state
    assert restored.original_text == original_text
    assert restored.reason_codes == ("rule:url", "原始原因:特殊字符😀")
    assert restored.confidence == 0.75
    # 原始文本不转义、保留 Unicode
    assert "<b>不转义</b>" in raw
    assert "😀" in raw
    # 默认生成的 review_id 为 16 位小写 hex
    assert re.fullmatch(r"[0-9a-f]{16}", state.review_id)


def test_from_json_rejects_missing_field() -> None:
    payload = _payload()
    del payload["recognized_text"]

    with pytest.raises(ValueError):
        SpamReviewState.from_json(json.dumps(payload))


def test_from_json_rejects_unknown_field() -> None:
    payload = _payload()
    payload["unexpected"] = True

    with pytest.raises(ValueError):
        SpamReviewState.from_json(json.dumps(payload))


def test_from_json_rejects_duplicate_field() -> None:
    # object_pairs_hook 拒绝重复键：手工拼接含重复 review_id 的 JSON
    base = _state().to_json()
    duplicated = base.replace(
        '"review_id":"0123456789abcdef",',
        '"review_id":"0123456789abcdef","review_id":"0123456789abcdef",',
    )

    with pytest.raises(ValueError):
        SpamReviewState.from_json(duplicated)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("schema_version", "v2"),
        ("confidence", float("nan")),
        ("confidence", -0.01),
        ("confidence", 1.01),
        ("message_type", "video"),
        ("review_id", "not-16-hex"),
        ("reason_codes", ["rule:url", 123]),
        ("reason_codes", "rule:url"),
    ],
)
def test_from_json_rejects_invalid_field(
    field_name: str,
    invalid_value: object,
) -> None:
    payload = _payload()
    payload[field_name] = invalid_value

    with pytest.raises(ValueError):
        SpamReviewState.from_json(json.dumps(payload, allow_nan=True))


async def test_create_review_state_uses_nx_and_keeps_first_snapshot() -> None:
    redis = _FakeRedis()
    first = _state(original_text="first")
    second = _state(review_id="fedcba9876543210", original_text="edited later")
    key = RedisKeys.spam_review(-100, 123)

    with patch.object(spam_review, "get_redis", return_value=redis):
        assert await create_review_state(first, -100, 123) is first
        assert await create_review_state(second, -100, 123) is None

    assert redis.values[key] == first.to_json()
    assert redis.expirations[key] == 86400


async def test_create_review_state_respects_custom_ttl() -> None:
    """handler 传 ttl=spam_review_prompt_auto_delete_seconds，覆盖默认 86400。"""
    redis = _FakeRedis()
    state = _state()
    key = RedisKeys.spam_review(-100, 123)

    with patch.object(spam_review, "get_redis", return_value=redis):
        assert await create_review_state(state, -100, 123, ttl=3600) is state

    assert redis.expirations[key] == 3600


async def test_get_review_state_returns_state_or_none() -> None:
    redis = _FakeRedis()
    state = _state()
    key = RedisKeys.spam_review(-100, 123)

    with patch.object(spam_review, "get_redis", return_value=redis):
        redis.values[key] = state.to_json()
        assert await get_review_state(-100, 123) == state

        redis.values[key] = "{invalid-json"
        assert await get_review_state(-100, 123) is None

        del redis.values[key]
        assert await get_review_state(-100, 123) is None


async def test_consume_review_state_deletes_matching_state() -> None:
    redis = _FakeRedis()
    state = _state()
    key = RedisKeys.spam_review(-100, 123)
    redis.values[key] = state.to_json()

    with patch.object(spam_review, "get_redis", return_value=redis):
        consumed = await consume_review_state(-100, 123, state.review_id)

    assert consumed == state
    assert key not in redis.values


async def test_consume_review_state_returns_but_keeps_mismatched_state() -> None:
    redis = _FakeRedis()
    state = _state()
    key = RedisKeys.spam_review(-100, 123)
    redis.values[key] = state.to_json()

    with patch.object(spam_review, "get_redis", return_value=redis):
        current = await consume_review_state(-100, 123, "fedcba9876543210")

    assert current == state
    assert redis.values[key] == state.to_json()


async def test_consume_review_state_returns_none_when_missing() -> None:
    redis = _FakeRedis()

    with patch.object(spam_review, "get_redis", return_value=redis):
        assert await consume_review_state(-100, 123, "0123456789abcdef") is None


async def test_delete_review_state_if_match() -> None:
    redis = _FakeRedis()
    state = _state()
    key = RedisKeys.spam_review(-100, 123)
    redis.values[key] = state.to_json()

    with patch.object(spam_review, "get_redis", return_value=redis):
        assert not await delete_review_state_if_match(-100, 123, "fedcba9876543210")
        assert key in redis.values

        assert await delete_review_state_if_match(-100, 123, state.review_id)
        assert key not in redis.values


async def test_review_lock_acquires_and_releases() -> None:
    redis = _FakeRedis()
    key = RedisKeys.spam_review_lock(-100, 123)

    with patch.object(spam_review, "get_redis", return_value=redis):
        async with review_lock(-100, 123, ttl=60) as acquired:
            assert acquired is True
            assert key in redis.values
            assert redis.expirations[key] == 60

    assert key not in redis.values


async def test_review_lock_yields_false_when_already_held() -> None:
    redis = _FakeRedis()
    key = RedisKeys.spam_review_lock(-100, 123)
    redis.values[key] = "another-owner"

    with patch.object(spam_review, "get_redis", return_value=redis):
        async with review_lock(-100, 123) as acquired:
            assert acquired is False

    # 未取得锁时不释放他人持有的锁
    assert redis.values[key] == "another-owner"


async def test_review_lock_releases_on_exception() -> None:
    redis = _FakeRedis()
    key = RedisKeys.spam_review_lock(-100, 123)

    with (
        patch.object(spam_review, "get_redis", return_value=redis),
        pytest.raises(RuntimeError, match="boom"),
    ):
        async with review_lock(-100, 123) as acquired:
            assert acquired is True
            raise RuntimeError("boom")

    assert key not in redis.values
