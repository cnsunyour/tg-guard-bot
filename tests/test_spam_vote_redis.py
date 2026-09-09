"""集体投票会话 Lua 状态机的真实 Redis 集成测试。

单元测试用 _FakeRedis 模拟 Lua 语义，证明不了脚本在真 Redis 上的行为：
RESP 多值 table 返回的编码、HEXISTS/HINCRBY 原子性、并发下的一人一票、
consume 的 get-match-del 单胜者、TTL 固定窗口不随投票续期、过期后写入
不复活。本文件跑真实 Redis 补上这段缺口。

运行方式：URL 由 conftest 的 ``real_redis_url`` fixture 决定（见
test_verification_hint_redis.py 模块注释）：

    pytest tests/test_spam_vote_redis.py -m integration
"""

import asyncio

import pytest
import redis.asyncio as aioredis

from src.core import redis as core_redis
from src.core.redis import RedisKeys
from src.services.spam_vote import (
    SpamVoteSession,
    SpamVoteSource,
    cast_vote,
    consume_vote_session,
    create_vote_session,
    discard_vote_session,
    get_vote_prompt,
    record_vote_prompt,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHAT_ID = -1009999999999
ORIG_MSG_ID = 888001
VOTE_KEY = RedisKeys.spam_vote(CHAT_ID, ORIG_MSG_ID)


def _session(threshold: int = 5) -> SpamVoteSession:
    return SpamVoteSession(
        source=SpamVoteSource.report,
        offender_user_id=42,
        report_id=7,
        threshold=threshold,
        sample_text="加微信 xxx",
    )


@pytest.fixture
async def redis_client(monkeypatch, real_redis_url):
    """连上真实 Redis 并注入为全局客户端（可用性由 real_redis_url 保证）。"""
    client = aioredis.Redis.from_url(real_redis_url, decode_responses=True)
    monkeypatch.setattr(core_redis, "_redis_client", client)
    await client.delete(VOTE_KEY)
    try:
        yield client
    finally:
        await client.delete(VOTE_KEY)
        await client.aclose()


async def test_create_sets_ttl_and_keeps_first_session(redis_client) -> None:
    """创建原子设 TTL；同键重复创建返回 None 且不重置 TTL。"""
    first = _session()
    second = SpamVoteSession(
        source=SpamVoteSource.review,
        offender_user_id=42,
        report_id=None,
        threshold=5,
        sample_text="later",
    )

    assert await create_vote_session(first, CHAT_ID, ORIG_MSG_ID, ttl=120) is first
    assert await create_vote_session(second, CHAT_ID, ORIG_MSG_ID, ttl=5) is None

    # 重复创建不重置 TTL（首会话窗口保持 120 附近，而非被 5 覆盖）
    assert 100 <= await redis_client.ttl(VOTE_KEY) <= 120
    assert await redis_client.hget(VOTE_KEY, "_meta") == first.to_json()


async def test_cast_vote_resp_encoding_and_direction_lock(redis_client) -> None:
    """真实 Lua 返回 {status, up, down} 列表（RESP 编码）；一人一票方向固定。"""
    session = _session()
    await create_vote_session(session, CHAT_ID, ORIG_MSG_ID, ttl=60)

    outcome = await cast_vote(CHAT_ID, ORIG_MSG_ID, 1001, "up", expected_vote_id=session.vote_id)
    assert (outcome.status, outcome.up, outcome.down) == ("voted", 1, 0)

    outcome = await cast_vote(CHAT_ID, ORIG_MSG_ID, 1002, "down")
    assert (outcome.status, outcome.up, outcome.down) == ("voted", 1, 1)

    # 同一用户换方向重复投票：already，计数与已投方向不变
    outcome = await cast_vote(CHAT_ID, ORIG_MSG_ID, 1001, "down")
    assert (outcome.status, outcome.up, outcome.down) == ("already", 1, 1)

    assert await redis_client.hget(VOTE_KEY, "u1001") == "1"
    assert await redis_client.hget(VOTE_KEY, "_up") == "1"
    assert await redis_client.hget(VOTE_KEY, "_down") == "1"


async def test_concurrent_votes_count_exactly_once(redis_client) -> None:
    """并发投票：每人一票恰好计入一次（Lua 原子性）。"""
    session = _session(threshold=100)
    await create_vote_session(session, CHAT_ID, ORIG_MSG_ID, ttl=60)

    outcomes = await asyncio.gather(
        *[cast_vote(CHAT_ID, ORIG_MSG_ID, 2000 + i, "up") for i in range(10)]
    )
    assert all(o.status == "voted" for o in outcomes)
    # 每次投票观察到自己执行时刻的计数：串行化为 1..10 的排列（无一丢失/重复）
    assert sorted(o.up for o in outcomes) == list(range(1, 11))
    assert int(await redis_client.hget(VOTE_KEY, "_up")) == 10


async def test_concurrent_consume_single_winner(redis_client) -> None:
    """并发消费：get-match-del 全局至多成功一次。"""
    session = _session()
    await create_vote_session(session, CHAT_ID, ORIG_MSG_ID, ttl=60)

    consumed = await asyncio.gather(
        *[consume_vote_session(CHAT_ID, ORIG_MSG_ID, session.vote_id) for _ in range(5)]
    )
    winners = [c for c in consumed if c is not None]
    assert len(winners) == 1
    assert winners[0] == session
    assert await redis_client.exists(VOTE_KEY) == 0


async def test_expired_session_does_not_revive(redis_client) -> None:
    """会话过期后投票返回 no_session，record_vote_prompt 拒绝写入（无 TTL 复活键）。"""
    session = _session()
    await create_vote_session(session, CHAT_ID, ORIG_MSG_ID, ttl=1)
    await redis_client.expire(VOTE_KEY, 0)  # 立即过期
    assert await redis_client.exists(VOTE_KEY) == 0

    outcome = await cast_vote(CHAT_ID, ORIG_MSG_ID, 1001, "up")
    assert outcome.status == "no_session"

    assert await record_vote_prompt(CHAT_ID, ORIG_MSG_ID, 555, "base") is False
    assert await redis_client.exists(VOTE_KEY) == 0


async def test_vote_does_not_extend_ttl(redis_client) -> None:
    """投票不续期：固定窗口从创建起算。"""
    session = _session()
    await create_vote_session(session, CHAT_ID, ORIG_MSG_ID, ttl=50)
    ttl_before = await redis_client.ttl(VOTE_KEY)

    await cast_vote(CHAT_ID, ORIG_MSG_ID, 1001, "up")
    ttl_after = await redis_client.ttl(VOTE_KEY)

    assert ttl_after <= ttl_before


async def test_discard_and_consume_mismatch_semantics(redis_client) -> None:
    """discard 无条件删除；consume 的 vote_id 不匹配保留会话（重建场景）。"""
    session = _session()
    await create_vote_session(session, CHAT_ID, ORIG_MSG_ID, ttl=60)

    mismatched = await consume_vote_session(CHAT_ID, ORIG_MSG_ID, "fedcba9876543210")
    assert mismatched == session
    assert await redis_client.exists(VOTE_KEY) == 1

    await discard_vote_session(CHAT_ID, ORIG_MSG_ID)
    assert await redis_client.exists(VOTE_KEY) == 0


async def test_consume_removes_prompt_location_read_before(redis_client) -> None:
    """consume 整键删除含提示定位——终局路径必须在消费**前**读取 _prompt 字段。

    回归契约：finalize_vote_if_ready 曾在消费后才读 get_vote_prompt，导致
    结果文案永远无法编辑进提示消息。
    """
    session = _session()
    await create_vote_session(session, CHAT_ID, ORIG_MSG_ID, ttl=60)
    await record_vote_prompt(CHAT_ID, ORIG_MSG_ID, 555, "🔔 提示正文")

    # 消费前可读
    assert await get_vote_prompt(CHAT_ID, ORIG_MSG_ID) == (555, "🔔 提示正文")

    await consume_vote_session(CHAT_ID, ORIG_MSG_ID, session.vote_id)
    # 消费后整键消失（含提示定位）
    assert await get_vote_prompt(CHAT_ID, ORIG_MSG_ID) is None
