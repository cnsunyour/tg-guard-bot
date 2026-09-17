"""群成员集体投票会话基础设施测试。

测试策略：``_FakeRedis`` 模拟 redis-py 的 ``eval`` / ``hget`` / ``hmget`` / ``delete``
调用契约（含 Lua 脚本语义），验证 Python 侧的会话状态机与调用方式；**不等同于真实
Redis Lua 集成测试**，真实 Redis 行为由 ``test_spam_vote_redis.py`` 覆盖。
"""

import json
import re
from unittest.mock import patch

import pytest

from src.core.redis import RedisKeys
from src.services import spam_vote
from src.services.spam_vote import (
    SpamVoteSession,
    SpamVoteSource,
    cast_vote,
    consume_vote_session,
    create_vote_session,
    discard_vote_session,
    get_vote_prompt,
    get_vote_session,
    record_vote_prompt,
)

pytestmark = pytest.mark.unit


def _session(
    *,
    vote_id: str = "0123456789abcdef",
    source: SpamVoteSource = SpamVoteSource.review,
    sample_text: str = "加微信 xxx 低价 VPN",
) -> SpamVoteSession:
    return SpamVoteSession(
        vote_id=vote_id,
        source=source,
        offender_user_id=42,
        report_id=7 if source == SpamVoteSource.report else None,
        threshold=5,
        sample_text=sample_text,
    )


def _payload() -> dict[str, object]:
    return json.loads(_session().to_json())


class _FakeRedis:
    """用于验证 Redis 调用边界的最小内存替身（模拟 HASH 与 Lua 脚本语义）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int | None] = {}

    def _drop(self, key: str) -> int:
        if key not in self.hashes:
            return 0
        del self.hashes[key]
        self.expirations.pop(key, None)
        return 1

    async def eval(
        self,
        script: str,
        numkeys: int,
        key: str,
        *args: str,
    ) -> list[int] | int | str | bool | None:
        assert numkeys == 1

        if script == spam_vote._CREATE_VOTE_SCRIPT:
            if key in self.hashes:
                return 0
            self.hashes[key] = {"_meta": args[0]}
            self.expirations[key] = int(args[1])
            return 1

        if script == spam_vote._VOTE_SCRIPT:
            fields = self.hashes.get(key) or {}
            meta = fields.get("_meta")
            if meta is None:
                return [-2, 0, 0]
            if args[2] != "" and json.loads(meta)["vote_id"] != args[2]:
                return [-3, 0, 0]
            up = int(fields.get("_up", "0"))
            down = int(fields.get("_down", "0"))
            user_field = f"u{args[0]}"
            if user_field in fields:
                return [0, up, down]
            if args[1] == "up":
                fields[user_field] = "1"
                fields["_up"] = str(up + 1)
                return [1, up + 1, down]
            fields[user_field] = "-1"
            fields["_down"] = str(down + 1)
            return [1, up, down + 1]

        if script == spam_vote._RECORD_PROMPT_SCRIPT:
            fields = self.hashes.get(key)
            if fields is None:
                return 0
            fields["_prompt_id"] = args[0]
            fields["_prompt_base"] = args[1]
            return 1

        if script == spam_vote._CONSUME_VOTE_SCRIPT:
            fields = self.hashes.get(key) or {}
            meta = fields.get("_meta")
            if meta is None:
                return False
            if json.loads(meta)["vote_id"] == args[0]:
                self._drop(key)
            return meta

        raise AssertionError("unexpected Lua script")

    async def hget(self, key: str, field_name: str) -> str | None:
        return self.hashes.get(key, {}).get(field_name)

    async def hmget(self, key: str, *field_names: str) -> list[str | None]:
        fields = self.hashes.get(key, {})
        return [fields.get(name) for name in field_names]

    async def delete(self, key: str) -> int:
        return self._drop(key)


def test_vote_source_values() -> None:
    assert [source.value for source in SpamVoteSource] == ["review", "report"]


def test_vote_key() -> None:
    assert RedisKeys.spam_vote(-100, 123) == "spam_vote:-100:123"


def test_to_json_from_json_roundtrip() -> None:
    session = _session(sample_text='中文 <b>不转义</b> 😀 "quoted"')
    restored = SpamVoteSession.from_json(session.to_json())

    assert restored == session
    # 文本不转义、保留 Unicode；默认生成的 vote_id 为 16 位小写 hex
    assert "😀" in session.to_json()
    assert re.fullmatch(r"[0-9a-f]{16}", SpamVoteSession.__dataclass_fields__["vote_id"].default_factory())  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutation", "invalid_value"),
    [
        ("schema_version", "v2"),
        ("vote_id", "not-16-hex"),
        ("source", "unknown"),
        ("offender_user_id", True),
        ("report_id", "7"),
        ("threshold", 1),
        ("sample_text", 123),
    ],
)
def test_from_json_rejects_invalid_field(
    mutation: str,
    invalid_value: object,
) -> None:
    payload = _payload()
    payload[mutation] = invalid_value

    with pytest.raises((ValueError, TypeError)):
        SpamVoteSession.from_json(json.dumps(payload))


def test_from_json_rejects_missing_field() -> None:
    payload = _payload()
    del payload["report_id"]

    with pytest.raises(ValueError):
        SpamVoteSession.from_json(json.dumps(payload))


def test_from_json_rejects_unknown_field() -> None:
    payload = _payload()
    payload["unexpected"] = True

    with pytest.raises(ValueError):
        SpamVoteSession.from_json(json.dumps(payload))


def test_from_json_rejects_duplicate_field() -> None:
    # object_pairs_hook 拒绝重复键：手工拼接含重复 vote_id 的 JSON
    base = _session().to_json()
    duplicated = base.replace(
        '"vote_id":"0123456789abcdef",',
        '"vote_id":"0123456789abcdef","vote_id":"0123456789abcdef",',
    )

    with pytest.raises(ValueError):
        SpamVoteSession.from_json(duplicated)


async def test_create_vote_session_keeps_first_session() -> None:
    redis = _FakeRedis()
    first = _session(sample_text="first")
    second = _session(vote_id="fedcba9876543210", sample_text="rebuilt later")
    key = RedisKeys.spam_vote(-100, 123)

    with patch.object(spam_vote, "get_redis", return_value=redis):
        assert await create_vote_session(first, -100, 123, ttl=3600) is first
        assert await create_vote_session(second, -100, 123, ttl=60) is None

    assert redis.hashes[key]["_meta"] == first.to_json()
    # 重复创建不重置 TTL（首会话的窗口保持不变）
    assert redis.expirations[key] == 3600


async def test_get_vote_session_returns_session_or_none() -> None:
    redis = _FakeRedis()
    session = _session()
    key = RedisKeys.spam_vote(-100, 123)

    with patch.object(spam_vote, "get_redis", return_value=redis):
        redis.hashes[key] = {"_meta": session.to_json()}
        assert await get_vote_session(-100, 123) == session

        redis.hashes[key] = {"_meta": "{invalid-json"}
        assert await get_vote_session(-100, 123) is None

        del redis.hashes[key]
        assert await get_vote_session(-100, 123) is None


async def test_record_and_get_vote_prompt() -> None:
    redis = _FakeRedis()
    session = _session()
    key = RedisKeys.spam_vote(-100, 123)
    redis.hashes[key] = {"_meta": session.to_json()}

    with patch.object(spam_vote, "get_redis", return_value=redis):
        assert await record_vote_prompt(-100, 123, 555, "🔔 提示正文") is True
        assert await get_vote_prompt(-100, 123) == (555, "🔔 提示正文")

    # 会话不存在时拒绝写入（防过期键复活），读取返回 None
    del redis.hashes[key]
    with patch.object(spam_vote, "get_redis", return_value=redis):
        assert await record_vote_prompt(-100, 123, 556, "x") is False
        assert await get_vote_prompt(-100, 123) is None


async def test_cast_vote_counts_and_locks_direction() -> None:
    redis = _FakeRedis()
    session = _session()
    key = RedisKeys.spam_vote(-100, 123)
    redis.hashes[key] = {"_meta": session.to_json()}

    with patch.object(spam_vote, "get_redis", return_value=redis):
        outcome = await cast_vote(-100, 123, 1001, "up", expected_vote_id=session.vote_id)
        assert (outcome.status, outcome.up, outcome.down) == ("voted", 1, 0)

        outcome = await cast_vote(-100, 123, 1002, "down", expected_vote_id=session.vote_id)
        assert (outcome.status, outcome.up, outcome.down) == ("voted", 1, 1)

        # 同一用户反向下重复投票：仅第一次有效，方向固定、计数不变
        outcome = await cast_vote(-100, 123, 1001, "down", expected_vote_id=session.vote_id)
        assert (outcome.status, outcome.up, outcome.down) == ("already", 1, 1)

    assert redis.hashes[key]["u1001"] == "1"


async def test_cast_vote_without_session_returns_no_session() -> None:
    redis = _FakeRedis()

    with patch.object(spam_vote, "get_redis", return_value=redis):
        outcome = await cast_vote(-100, 123, 1001, "up")
        assert (outcome.status, outcome.up, outcome.down) == ("no_session", 0, 0)


async def test_cast_vote_rejects_mismatched_vote_id() -> None:
    redis = _FakeRedis()
    redis.hashes[RedisKeys.spam_vote(-100, 123)] = {"_meta": _session().to_json()}

    with patch.object(spam_vote, "get_redis", return_value=redis):
        outcome = await cast_vote(-100, 123, 1001, "up", expected_vote_id="fedcba9876543210")
        assert outcome.status == "mismatch"


async def test_consume_vote_session_deletes_matching_session() -> None:
    redis = _FakeRedis()
    session = _session()
    key = RedisKeys.spam_vote(-100, 123)
    redis.hashes[key] = {"_meta": session.to_json(), "u1001": "1", "_up": "1"}

    with patch.object(spam_vote, "get_redis", return_value=redis):
        consumed = await consume_vote_session(-100, 123, session.vote_id)

    assert consumed == session
    assert key not in redis.hashes


async def test_consume_vote_session_keeps_mismatched_session() -> None:
    redis = _FakeRedis()
    session = _session()
    key = RedisKeys.spam_vote(-100, 123)
    redis.hashes[key] = {"_meta": session.to_json()}

    with patch.object(spam_vote, "get_redis", return_value=redis):
        current = await consume_vote_session(-100, 123, "fedcba9876543210")

    assert current == session
    assert key in redis.hashes


async def test_consume_vote_session_returns_none_when_missing() -> None:
    redis = _FakeRedis()

    with patch.object(spam_vote, "get_redis", return_value=redis):
        assert await consume_vote_session(-100, 123, "0123456789abcdef") is None


async def test_discard_vote_session_unconditional() -> None:
    redis = _FakeRedis()
    key = RedisKeys.spam_vote(-100, 123)
    redis.hashes[key] = {"_meta": _session(vote_id="aaaabbbbccccdddd").to_json()}

    with patch.object(spam_vote, "get_redis", return_value=redis):
        # 不校验 vote_id：无论当前会话身份如何一律删除
        await discard_vote_session(-100, 123)
        assert key not in redis.hashes

        # 键不存在时静默
        await discard_vote_session(-100, 123)
