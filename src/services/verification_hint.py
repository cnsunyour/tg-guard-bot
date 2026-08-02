"""共享验证引导消息的 Redis 状态机。

并发模型：多用户同时未启动 Bot 时，同群同一 flow 30 秒内只发一条引导消息。

状态值两态：

- ``pending:{owner_token}``：某协程取得发送权，Telegram 消息尚未提交；
- ``message_id:{id}``：消息已发送，可以延长共享窗口。

所有状态变更均校验当前值（Lua CAS），避免：

- 发送期间 reservation 过期 → 旧 owner 覆盖新 owner 的状态；
- 发送失败协程误删他人 reservation；
- 崩溃残留的 pending 被删除任务或 try_extend 过度续命。

参考范式：``src/services/spam_review.py`` 的 Lua CAS。
"""

from __future__ import annotations

import secrets
from typing import Literal

from src.core.redis import RedisKeys, get_redis

type VerificationHintFlow = Literal["join", "join_request"]

_PENDING_PREFIX = "pending:"
_MESSAGE_ID_PREFIX = "message_id:"


def _pending_value(owner_token: str) -> str:
    return f"{_PENDING_PREFIX}{owner_token}"


# 仅允许 reservation owner 将自己的 pending 提升为已提交消息。
# KEEPTTL 保留 SET NX EX 建立的原始共享窗口（不重置 TTL）。
_PROMOTE_HINT_SCRIPT = """
local expected = ARGV[1]
if redis.call("get", KEYS[1]) == expected then
    redis.call("set", KEYS[1], ARGV[2], "KEEPTTL")
    return 1
end
return 0
""".strip()


# 发送失败时仅删除自己的 pending reservation（不误删他人或已提交状态）。
_DELETE_HINT_RESERVATION_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
""".strip()


# 只给已提交的 message_id 状态续期；pending 不续期（崩溃残留自然过期），
# 避免删除任务或并发协程为 pending 过度续命。
_EXTEND_HINT_SCRIPT = """
local raw = redis.call("get", KEYS[1])
if raw and string.sub(raw, 1, 11) == "message_id:" then
    return redis.call("expire", KEYS[1], ARGV[1])
end
return 0
""".strip()


async def reserve_hint(
    chat_id: int,
    flow: VerificationHintFlow,
    ttl: int = 30,
) -> str | None:
    """用 ``SET NX EX`` 竞争 hint 发送权。

    成功返回随机 owner token；已有 pending 或已提交状态时返回 None。
    """
    redis = get_redis()
    owner_token = secrets.token_hex(16)
    acquired = bool(
        await redis.set(
            RedisKeys.verification_hint(chat_id, flow),
            _pending_value(owner_token),
            nx=True,
            ex=ttl,
        )
    )
    return owner_token if acquired else None


async def promote_hint(
    chat_id: int,
    flow: VerificationHintFlow,
    owner_token: str,
    message_id: int,
) -> bool:
    """将自己的 pending reservation 原子提升为已提交消息。

    发送期间 reservation 过期或被替换时返回 False（调用方应删除未受状态机
    管理的 Telegram 消息，避免第二条 hint 残留）。
    """
    redis = get_redis()
    promoted = await redis.eval(
        _PROMOTE_HINT_SCRIPT,
        1,
        RedisKeys.verification_hint(chat_id, flow),
        _pending_value(owner_token),
        f"{_MESSAGE_ID_PREFIX}{message_id}",
    )
    return bool(promoted)


async def delete_hint_reservation(
    chat_id: int,
    flow: VerificationHintFlow,
    owner_token: str,
) -> bool:
    """发送失败时仅删除调用方自己的 pending reservation。"""
    redis = get_redis()
    deleted = await redis.eval(
        _DELETE_HINT_RESERVATION_SCRIPT,
        1,
        RedisKeys.verification_hint(chat_id, flow),
        _pending_value(owner_token),
    )
    return bool(deleted)


async def try_extend_hint(
    chat_id: int,
    flow: VerificationHintFlow,
    ttl: int = 30,
) -> bool:
    """已提交 message_id 存在时延长 TTL；pending 或 key 不存在返回 False。"""
    redis = get_redis()
    extended = await redis.eval(
        _EXTEND_HINT_SCRIPT,
        1,
        RedisKeys.verification_hint(chat_id, flow),
        ttl,
    )
    return bool(extended)
