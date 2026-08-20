"""验证引导消息 Redis 状态机测试。

测试策略：_FakeRedis 模拟 set/get/eval（含 Lua 脚本语义），验证 Python 侧状态机
逻辑；不等同于真实 Redis Lua 集成测试（真机验证在 3c1-2 接入后）。
"""

import pytest

from src.core.redis import RedisKeys
from src.services import verification_hint
from src.services.verification_hint import (
    add_hint_user,
    claim_hint_edit,
    claim_hint_render,
    delete_hint_reservation,
    get_hint_ttl_if_match,
    promote_hint,
    reserve_hint,
    snapshot_hint_users,
    try_extend_hint,
)

pytestmark = pytest.mark.unit

CHAT_ID = -100
FLOW = "join"

HINT_KEY = RedisKeys.verification_hint(CHAT_ID, FLOW)
USERS_KEY = RedisKeys.verification_hint_users(CHAT_ID, FLOW)
RENDER_KEY = RedisKeys.verification_hint_render(CHAT_ID, FLOW)


class _FakeRedis:
    """模拟 redis-py 调用契约 + Lua 脚本语义（含 ZSET）。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
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

    # --- Lua 内使用的原语（由 eval 分支调用，模拟 redis.call 行为） ---

    def _delete(self, *keys) -> None:
        for key in keys:
            self.values.pop(key, None)
            self.sorted_sets.pop(key, None)
            self.expirations.pop(key, None)

    def _expire(self, key, ttl) -> None:
        if key in self.values or key in self.sorted_sets:
            self.expirations[key] = int(ttl)

    def _zadd_nx(self, key, score, member) -> int:
        members = self.sorted_sets.setdefault(key, {})
        if member in members:
            return 0
        members[member] = float(score)
        return 1

    def _zcard(self, key) -> int:
        return len(self.sorted_sets.get(key, {}))

    def _zrange(self, key, start, stop) -> list[str]:
        members = self.sorted_sets.get(key, {})
        ordered = sorted(members.items(), key=lambda item: (item[1], item[0]))
        return [member for member, _ in ordered[start : stop + 1]]

    async def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]

        if script == verification_hint._RESERVE_HINT_SCRIPT:
            hint_key, users_key, render_key = keys
            if hint_key in self.values:
                return 0
            self.values[hint_key] = argv[0]
            self.expirations[hint_key] = int(argv[1])
            self._delete(users_key, render_key)
            return 1

        if script == verification_hint._ADD_HINT_USER_SCRIPT:
            hint_key, users_key = keys
            hint = self.values.get(hint_key)
            if hint is None:
                return [0, "", 0]
            added = self._zadd_nx(users_key, self._zcard(users_key), str(argv[0]))
            self._expire(users_key, argv[1])
            return [added, hint, self._zcard(users_key)]

        if script == verification_hint._SNAPSHOT_HINT_USERS_SCRIPT:
            users_key = keys[0]
            total = self._zcard(users_key)
            limit = int(argv[0])
            if total == 0 or limit <= 0:
                return [total, []]
            return [total, self._zrange(users_key, 0, limit - 1)]

        if script == verification_hint._CLAIM_HINT_RENDER_SCRIPT:
            hint_key, render_key = keys
            if self.values.get(hint_key) != argv[0]:
                return 0
            current = int(self.values.get(render_key, "0"))
            if int(argv[1]) <= current:
                return 0
            self.values[render_key] = str(argv[1])
            self.expirations[render_key] = int(argv[2])
            return 1

        if script == verification_hint._CLAIM_HINT_EDIT_SCRIPT:
            hint_key, users_key, render_key = keys
            limit = int(argv[0])
            hint = self.values.get(hint_key)
            if not hint or not hint.startswith("message_id:") or limit <= 0:
                return ["", 0, []]
            total = self._zcard(users_key)
            version = min(total, limit)
            current = int(self.values.get(render_key, "0"))
            if version <= current:
                return ["", total, []]
            self.values[render_key] = str(version)
            self.expirations[render_key] = int(argv[1])
            return [hint, total, self._zrange(users_key, 0, limit - 1)]

        if script == verification_hint._PROMOTE_HINT_SCRIPT:
            # argv = (expected_pending, message_id_value)
            key = keys[0]
            if self.values.get(key) == argv[0]:
                self.values[key] = argv[1]
                return 1
            return 0

        if script == verification_hint._DELETE_HINT_RESERVATION_SCRIPT:
            # argv = (expected_pending,)
            key = keys[0]
            if self.values.get(key) == argv[0]:
                self._delete(key)
                return 1
            return 0

        if script == verification_hint._EXTEND_HINT_SCRIPT:
            # argv = (ttl,)
            hint_key, users_key, render_key = keys
            raw = self.values.get(hint_key)
            if raw and raw.startswith("message_id:"):
                for key in (hint_key, users_key, render_key):
                    self._expire(key, argv[0])
                return 1
            return 0

        if script == verification_hint._GET_HINT_TTL_IF_MATCH_SCRIPT:
            # argv = (expected_message_id_value,)
            key = keys[0]
            if self.values.get(key) != argv[0]:
                return -1
            ttl = self.expirations.get(key)
            if ttl is None or ttl <= 0:
                return -2
            return ttl

        raise AssertionError("unexpected Lua script")


def _patch_redis(mocker, redis):
    mocker.patch.object(verification_hint, "get_redis", return_value=redis)


async def test_get_hint_ttl_if_match_returns_ttl_only_for_own_message(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 999)
    redis.expirations[HINT_KEY] = 25

    # 仍指向 999 → 返回 TTL
    assert await get_hint_ttl_if_match(CHAT_ID, FLOW, 999) == 25

    # 指向其他 message_id → -1（不拖延，立即删）
    assert await get_hint_ttl_if_match(CHAT_ID, FLOW, 1000) == -1


async def test_get_hint_ttl_if_match_returns_minus_1_when_missing(mocker) -> None:
    """key 不存在或值不匹配均返回 -1（调用方立即删旧消息，不拖延）。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    # key 不存在 → -1
    assert await get_hint_ttl_if_match(CHAT_ID, FLOW, 999) == -1

    # 值不匹配（指向其他 message_id）→ -1
    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 888)
    assert await get_hint_ttl_if_match(CHAT_ID, FLOW, 999) == -1


async def test_reserve_hint_nx_returns_token_or_none(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    token1 = await reserve_hint(CHAT_ID, FLOW)
    assert token1 is not None
    assert redis.values[HINT_KEY] == f"pending:{token1}"

    # 二次 reserve（已存在）→ None
    token2 = await reserve_hint(CHAT_ID, FLOW)
    assert token2 is None


async def test_reserve_hint_clears_previous_window_state(mocker) -> None:
    """新窗口不得继承上一窗口的用户与渲染版本（否则旧用户会被 mention 进新消息）。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 777)
    await add_hint_user(CHAT_ID, FLOW, 10)
    await claim_hint_render(CHAT_ID, FLOW, 777, 1)

    # 上一窗口过期
    redis._delete(HINT_KEY)

    assert await reserve_hint(CHAT_ID, FLOW) is not None
    assert redis._zcard(USERS_KEY) == 0
    assert RENDER_KEY not in redis.values


async def test_reserve_hint_failure_keeps_other_window_state(mocker) -> None:
    """竞争失败者不能清空当前窗口的用户集合。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    await reserve_hint(CHAT_ID, FLOW)
    await add_hint_user(CHAT_ID, FLOW, 10)

    assert await reserve_hint(CHAT_ID, FLOW) is None
    assert redis._zcard(USERS_KEY) == 1


async def test_add_hint_user_dedupes_and_reports_commit_state(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)

    # pending 期间登记：新增，消息尚未发出
    assert await add_hint_user(CHAT_ID, FLOW, 10) == (True, False, 1)
    # 同一用户重复入群不重复占位
    assert await add_hint_user(CHAT_ID, FLOW, 10) == (False, False, 1)

    # 消息发出后登记：committed=True，调用方据此触发编辑补全
    await promote_hint(CHAT_ID, FLOW, token, 555)
    assert await add_hint_user(CHAT_ID, FLOW, 11) == (True, True, 2)


async def test_add_hint_user_rejects_expired_window(mocker) -> None:
    """窗口不存在时拒绝写入，避免孤立用户被下一个窗口继承。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    assert await add_hint_user(CHAT_ID, FLOW, 10) == (False, False, 0)
    assert redis._zcard(USERS_KEY) == 0


async def test_snapshot_hint_users_truncates_in_join_order(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    await reserve_hint(CHAT_ID, FLOW)

    for user_id in (30, 10, 20):
        await add_hint_user(CHAT_ID, FLOW, user_id)

    # 按加入顺序（非 user_id 大小）截断，总数反映溢出
    assert await snapshot_hint_users(CHAT_ID, FLOW, 2) == ([30, 10], 3)
    assert await snapshot_hint_users(CHAT_ID, FLOW, 5) == ([30, 10, 20], 3)
    assert await snapshot_hint_users(CHAT_ID, FLOW, 0) == ([], 3)


async def test_claim_hint_render_only_moves_forward(mocker) -> None:
    """版本单调递增：旧编辑不能覆盖已补全更多 mention 的消息。"""
    _patch_redis(mocker, _FakeRedis())
    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 4242)

    assert await claim_hint_render(CHAT_ID, FLOW, 4242, 2) is True
    assert await claim_hint_render(CHAT_ID, FLOW, 4242, 1) is False
    # 版本相等说明内容不变，无需编辑
    assert await claim_hint_render(CHAT_ID, FLOW, 4242, 2) is False
    assert await claim_hint_render(CHAT_ID, FLOW, 4242, 3) is True


async def test_claim_hint_render_rejects_other_message(mocker) -> None:
    """旧窗口协程不得写坏新窗口的渲染版本（hint 已指向别的消息）。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 4242)

    assert await claim_hint_render(CHAT_ID, FLOW, 1111, 9) is False
    assert RENDER_KEY not in redis.values


async def test_claim_hint_edit_returns_snapshot_and_bumps_version(mocker) -> None:
    """编辑权原子返回同一窗口的 message_id + 用户快照，并推进版本。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)

    # pending 期间不发编辑（这些用户由 owner 的首条消息带出）
    await add_hint_user(CHAT_ID, FLOW, 1001)
    assert await claim_hint_edit(CHAT_ID, FLOW, 5) is None

    await promote_hint(CHAT_ID, FLOW, token, 4242)
    await add_hint_user(CHAT_ID, FLOW, 1002)

    claim = await claim_hint_edit(CHAT_ID, FLOW, 5)
    assert claim is not None
    assert claim.message_id == 4242
    assert claim.mention_ids == [1001, 1002]
    assert claim.total == 2
    assert redis.values[RENDER_KEY] == "2"

    # 内容未变化 → 不重复编辑
    assert await claim_hint_edit(CHAT_ID, FLOW, 5) is None


async def test_claim_hint_edit_stops_at_mention_limit(mocker) -> None:
    """超出上限后渲染内容不再变化，编辑权不再发放（省 API 配额）。"""
    _patch_redis(mocker, _FakeRedis())
    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 4242)

    for user_id in range(1001, 1004):
        await add_hint_user(CHAT_ID, FLOW, user_id)
    claim = await claim_hint_edit(CHAT_ID, FLOW, 2)
    assert claim is not None
    assert claim.mention_ids == [1001, 1002]
    assert claim.total == 3

    # 第 4 个用户仍溢出上限 → 无需编辑
    await add_hint_user(CHAT_ID, FLOW, 1004)
    assert await claim_hint_edit(CHAT_ID, FLOW, 2) is None


async def test_claim_hint_edit_requires_committed_window(mocker) -> None:
    """窗口不存在或功能关闭（limit=0）时不得发放编辑权。"""
    _patch_redis(mocker, _FakeRedis())

    assert await claim_hint_edit(CHAT_ID, FLOW, 5) is None

    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 4242)
    await add_hint_user(CHAT_ID, FLOW, 1001)
    assert await claim_hint_edit(CHAT_ID, FLOW, 0) is None


async def test_promote_hint_only_promotes_own_reservation(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)

    # 自己的 token → 提升成功，TTL 保留
    assert await promote_hint(CHAT_ID, FLOW, token, 123) is True
    assert redis.values[HINT_KEY] == "message_id:123"

    # 他人 token → 不提升
    assert await promote_hint(CHAT_ID, FLOW, "other-token", 456) is False
    assert redis.values[HINT_KEY] == "message_id:123"


async def test_delete_hint_reservation_only_deletes_own(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)
    token = await reserve_hint(CHAT_ID, FLOW)

    # 他人 token → 不删
    assert await delete_hint_reservation(CHAT_ID, FLOW, "other-token") is False
    assert HINT_KEY in redis.values

    # 自己 token → 删
    assert await delete_hint_reservation(CHAT_ID, FLOW, token) is True
    assert HINT_KEY not in redis.values


async def test_try_extend_hint_only_extends_committed_message(mocker) -> None:
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    # pending 状态 → 不延长
    token = await reserve_hint(CHAT_ID, FLOW)
    assert await try_extend_hint(CHAT_ID, FLOW) is False

    # 提升为 message_id → 延长成功
    await promote_hint(CHAT_ID, FLOW, token, 999)
    assert await try_extend_hint(CHAT_ID, FLOW, ttl=60) is True
    assert redis.expirations[HINT_KEY] == 60

    # key 不存在 → False
    del redis.values[HINT_KEY]
    assert await try_extend_hint(CHAT_ID, FLOW) is False


async def test_try_extend_hint_keeps_side_keys_in_sync(mocker) -> None:
    """users/render 必须与 hint 同步续期，否则晚到用户会看不到先到者的 mention。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    token = await reserve_hint(CHAT_ID, FLOW)
    await promote_hint(CHAT_ID, FLOW, token, 999)
    await add_hint_user(CHAT_ID, FLOW, 10)
    await claim_hint_render(CHAT_ID, FLOW, 999, 1)

    assert await try_extend_hint(CHAT_ID, FLOW, ttl=60) is True
    assert redis.expirations[HINT_KEY] == 60
    assert redis.expirations[USERS_KEY] == 60
    assert redis.expirations[RENDER_KEY] == 60


def test_verification_hint_key_flow_validation() -> None:
    assert RedisKeys.verification_hint(-100, "join") == "verification_hint:join:-100"
    assert (
        RedisKeys.verification_hint(-100, "join_request") == "verification_hint:join_request:-100"
    )
    assert RedisKeys.verification_hint_users(-100, "join") == "verification_hint_users:join:-100"
    assert RedisKeys.verification_hint_render(-100, "join") == "verification_hint_render:join:-100"
    for factory in (
        RedisKeys.verification_hint,
        RedisKeys.verification_hint_users,
        RedisKeys.verification_hint_render,
    ):
        with pytest.raises(ValueError):
            factory(-100, "invalid")
