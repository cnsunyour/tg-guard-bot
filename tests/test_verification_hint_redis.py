"""验证引导消息 Lua 状态机的真实 Redis 集成测试。

单元测试用 _FakeRedis 模拟 Lua 语义，证明不了脚本本身在 Redis 上的行为
（ZADD NX 的 score 语义、嵌套 table 返回值的 RESP 编码、多 key EXPIRE、
并发下的原子性）。本文件跑真实 Redis，补上这段缺口。

运行方式：默认连 ``REDIS_TEST_URL``（缺省 redis://localhost:6379/15），
连不上则整体跳过，不阻塞 ``make test``：

    redis-server --port 6379 --daemonize yes
    pytest tests/test_verification_hint_redis.py -m integration
"""

import asyncio
import os

import pytest
import redis.asyncio as aioredis

from src.core import redis as core_redis
from src.core.redis import RedisKeys
from src.services.verification_hint import (
    add_hint_user,
    claim_hint_edit,
    claim_hint_render,
    promote_hint,
    reserve_hint,
    snapshot_hint_users,
    try_extend_hint,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHAT_ID = -1009999999999
FLOW = "join"
REDIS_URL = os.getenv("REDIS_TEST_URL", "redis://localhost:6379/15")

HINT_KEY = RedisKeys.verification_hint(CHAT_ID, FLOW)
USERS_KEY = RedisKeys.verification_hint_users(CHAT_ID, FLOW)
RENDER_KEY = RedisKeys.verification_hint_render(CHAT_ID, FLOW)


@pytest.fixture
async def redis_client(monkeypatch):
    """连上真实 Redis 并注入为全局客户端；连不上则跳过整个文件。"""
    client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        # 环境不可用即跳过，不区分错误类型
        await client.aclose()
        pytest.skip(f"真实 Redis 不可用（{REDIS_URL}）：{exc}")

    monkeypatch.setattr(core_redis, "_redis_client", client)
    await client.delete(HINT_KEY, USERS_KEY, RENDER_KEY)
    try:
        yield client
    finally:
        await client.delete(HINT_KEY, USERS_KEY, RENDER_KEY)
        await client.aclose()


async def test_reserve_clears_stale_window_and_sets_ttl(redis_client) -> None:
    """新窗口必须清空上一窗口残留，否则旧用户会被 mention 进新消息。"""
    await redis_client.zadd(USERS_KEY, {"999": 0})
    await redis_client.set(RENDER_KEY, "7")

    token = await reserve_hint(CHAT_ID, FLOW, ttl=32)

    assert token is not None
    assert await redis_client.zcard(USERS_KEY) == 0
    assert await redis_client.get(RENDER_KEY) is None
    assert await redis_client.ttl(HINT_KEY) == 32
    # 竞争失败者不得破坏当前窗口
    await add_hint_user(CHAT_ID, FLOW, 1001, ttl=32)
    assert await reserve_hint(CHAT_ID, FLOW) is None
    assert await redis_client.zcard(USERS_KEY) == 1


async def test_add_hint_user_scores_are_unique_and_ordered(redis_client) -> None:
    """score 取写入前 ZCARD：必须唯一递增，否则 ZRANGE 的加入顺序会错乱。"""
    await reserve_hint(CHAT_ID, FLOW, ttl=32)

    assert await add_hint_user(CHAT_ID, FLOW, 1001, ttl=32) == (True, False, 1)
    assert await add_hint_user(CHAT_ID, FLOW, 1001, ttl=32) == (False, False, 1)
    await add_hint_user(CHAT_ID, FLOW, 1002, ttl=32)
    await add_hint_user(CHAT_ID, FLOW, 1003, ttl=32)

    assert await redis_client.zrange(USERS_KEY, 0, -1, withscores=True) == [
        ("1001", 0.0),
        ("1002", 1.0),
        ("1003", 2.0),
    ]
    # 嵌套 Lua table 经 RESP 解码后仍能正确解析
    assert await snapshot_hint_users(CHAT_ID, FLOW, 2) == ([1001, 1002], 3)
    assert await snapshot_hint_users(CHAT_ID, FLOW, 0) == ([], 3)


async def test_add_hint_user_rejects_expired_window(redis_client) -> None:
    """窗口过期后拒绝写入，避免孤立用户被下一个窗口继承。"""
    token = await reserve_hint(CHAT_ID, FLOW, ttl=32)
    await promote_hint(CHAT_ID, FLOW, token, 4242)
    await redis_client.delete(HINT_KEY)

    assert await add_hint_user(CHAT_ID, FLOW, 1001, ttl=32) == (False, False, 0)
    assert await redis_client.exists(USERS_KEY) == 0
    assert await try_extend_hint(CHAT_ID, FLOW) is False


async def test_claim_hint_render_binds_to_message(redis_client) -> None:
    """初始版本提交必须绑定自己的 message_id，挡住旧窗口协程写坏新窗口版本。"""
    token = await reserve_hint(CHAT_ID, FLOW, ttl=32)
    await promote_hint(CHAT_ID, FLOW, token, 4242)

    assert await claim_hint_render(CHAT_ID, FLOW, 1111, 9) is False
    assert await redis_client.exists(RENDER_KEY) == 0

    assert await claim_hint_render(CHAT_ID, FLOW, 4242, 2) is True
    assert await claim_hint_render(CHAT_ID, FLOW, 4242, 2) is False
    assert await claim_hint_render(CHAT_ID, FLOW, 4242, 3) is True


async def test_claim_hint_edit_is_atomic_across_window(redis_client) -> None:
    """编辑权原子返回同窗口的 message_id + 快照，并按上限停止发放。"""
    token = await reserve_hint(CHAT_ID, FLOW, ttl=32)
    await add_hint_user(CHAT_ID, FLOW, 1001, ttl=32)
    # pending 期间不发编辑权（这批用户由 owner 首条消息带出）
    assert await claim_hint_edit(CHAT_ID, FLOW, 5) is None

    await promote_hint(CHAT_ID, FLOW, token, 4242)
    await add_hint_user(CHAT_ID, FLOW, 1002, ttl=32)

    claim = await claim_hint_edit(CHAT_ID, FLOW, 5)
    assert claim is not None
    assert (claim.message_id, claim.mention_ids, claim.total) == (4242, [1001, 1002], 2)

    # 内容未变化 → 不再发放
    assert await claim_hint_edit(CHAT_ID, FLOW, 5) is None
    # 溢出上限的新用户同样不触发编辑
    await add_hint_user(CHAT_ID, FLOW, 1003, ttl=32)
    assert await claim_hint_edit(CHAT_ID, FLOW, 2) is None


async def test_try_extend_hint_syncs_three_keys(redis_client) -> None:
    token = await reserve_hint(CHAT_ID, FLOW, ttl=32)
    await promote_hint(CHAT_ID, FLOW, token, 4242)
    await add_hint_user(CHAT_ID, FLOW, 1001, ttl=32)
    await claim_hint_render(CHAT_ID, FLOW, 4242, 1)

    assert await try_extend_hint(CHAT_ID, FLOW, ttl=60) is True
    assert await redis_client.ttl(HINT_KEY) == 60
    assert await redis_client.ttl(USERS_KEY) == 60
    assert await redis_client.ttl(RENDER_KEY) == 60


async def test_concurrent_joins_keep_single_owner_and_no_user_loss(redis_client) -> None:
    """30 人同时入群：发送权唯一、用户不丢不重、score 无冲突。"""
    concurrency = 30

    async def one(user_id: int) -> str | None:
        token = await reserve_hint(CHAT_ID, FLOW, ttl=32)
        await add_hint_user(CHAT_ID, FLOW, user_id, ttl=32)
        return token

    tokens = await asyncio.gather(*(one(2000 + i) for i in range(concurrency)))

    assert len([t for t in tokens if t is not None]) == 1
    assert await redis_client.zcard(USERS_KEY) == concurrency
    scores = [s for _, s in await redis_client.zrange(USERS_KEY, 0, -1, withscores=True)]
    assert len(set(scores)) == concurrency

    mention_ids, total = await snapshot_hint_users(CHAT_ID, FLOW, 5)
    assert total == concurrency
    assert len(set(mention_ids)) == 5
