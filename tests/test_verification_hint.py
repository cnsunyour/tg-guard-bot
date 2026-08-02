"""验证引导消息 Redis 状态机测试。

测试策略：_FakeRedis 模拟 set/get/eval（含 Lua 脚本语义），验证 Python 侧状态机
逻辑；不等同于真实 Redis Lua 集成测试（真机验证在 3c1-2 接入后）。
"""

import pytest

from src.core.redis import RedisKeys
from src.services import verification_hint
from src.services.verification_hint import (
    delete_hint_reservation,
    promote_hint,
    reserve_hint,
    try_extend_hint,
)

pytestmark = pytest.mark.unit

CHAT_ID = -100
FLOW = "join"


class _FakeRedis:
    """模拟 redis-py 调用契约 + Lua 脚本语义。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int | None] = {}

    async def set(self, key, value, *, nx=False, ex=None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def get(self, key) -> str | None:
        return self.values.get(key)

    async def ttl(self, key) -> int:
        return self.expirations.get(key) or -2

    async def eval(self, script, numkeys, key, *args) -> int:
        assert numkeys == 1
        if script == verification_hint._PROMOTE_HINT_SCRIPT:
            # args = (expected_pending, message_id_value)
            if self.values.get(key) == args[0]:
                self.values[key] = args[1]
                return 1
            return 0
        if script == verification_hint._DELETE_HINT_RESERVATION_SCRIPT:
            # args = (expected_pending,)
            if self.values.get(key) == args[0]:
                del self.values[key]
                self.expirations.pop(key, None)
                return 1
            return 0
        if script == verification_hint._EXTEND_HINT_SCRIPT:
            # args = (ttl,)
            raw = self.values.get(key)
            if raw and raw.startswith("message_id:"):
                self.expirations[key] = int(args[0])
                return 1
            return 0
        raise AssertionError("unexpected Lua script")


def _patch_redis(mocker, redis):
    mocker.patch.object(verification_hint, "get_redis", return_value=redis)


async def test_reserve_hint_nx_returns_token_or_none(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    token1 = await reserve_hint(CHAT_ID, FLOW)
    assert token1 is not None
    assert redis.values[RedisKeys.verification_hint(CHAT_ID, FLOW)] == f"pending:{token1}"

    # 二次 reserve（已存在）→ None
    token2 = await reserve_hint(CHAT_ID, FLOW)
    assert token2 is None


async def test_promote_hint_only_promotes_own_reservation(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)

    # 自己的 token → 提升成功，TTL 保留
    assert await promote_hint(CHAT_ID, FLOW, token, 123) is True
    assert redis.values[RedisKeys.verification_hint(CHAT_ID, FLOW)] == "message_id:123"

    # 他人 token → 不提升
    assert await promote_hint(CHAT_ID, FLOW, "other-token", 456) is False
    assert redis.values[RedisKeys.verification_hint(CHAT_ID, FLOW)] == "message_id:123"


async def test_delete_hint_reservation_only_deletes_own(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)
    key = RedisKeys.verification_hint(CHAT_ID, FLOW)

    # 他人 token → 不删
    assert await delete_hint_reservation(CHAT_ID, FLOW, "other-token") is False
    assert key in redis.values

    # 自己 token → 删
    assert await delete_hint_reservation(CHAT_ID, FLOW, token) is True
    assert key not in redis.values


async def test_try_extend_hint_only_extends_committed_message(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    key = RedisKeys.verification_hint(CHAT_ID, FLOW)

    # pending 状态 → 不延长
    token = await reserve_hint(CHAT_ID, FLOW)
    assert await try_extend_hint(CHAT_ID, FLOW) is False

    # 提升为 message_id → 延长成功
    await promote_hint(CHAT_ID, FLOW, token, 999)
    assert await try_extend_hint(CHAT_ID, FLOW, ttl=60) is True
    assert redis.expirations[key] == 60

    # key 不存在 → False
    del redis.values[key]
    assert await try_extend_hint(CHAT_ID, FLOW) is False


def test_verification_hint_key_flow_validation() -> None:
    assert RedisKeys.verification_hint(-100, "join") == "verification_hint:join:-100"
    assert (
        RedisKeys.verification_hint(-100, "join_request") == "verification_hint:join_request:-100"
    )
    with pytest.raises(ValueError):
        RedisKeys.verification_hint(-100, "invalid")
