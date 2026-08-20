"""共享验证引导消息的 Redis 状态机。

并发模型：多用户同时未启动 Bot 时，同群同一 flow 30 秒内只发一条引导消息。

状态值两态：

- ``pending:{owner_token}``：某协程取得发送权，Telegram 消息尚未提交；
- ``message_id:{id}``：消息已发送，可以延长共享窗口。

窗口另有两个附属键（键名见 ``RedisKeys.verification_hint_users`` /
``verification_hint_render``），用于在引导消息里匿名 mention 等待验证的用户：

- users（ZSET）：窗口内待 mention 的 user_id，score 为加入序号；
- render（String）：已渲染进消息的 mention 数，作编辑的单调递增 CAS 版本。

两个附属键不独立存活——取得发送权时清空、窗口续期时一并续期，确保上一窗口
的用户不会出现在下一条引导消息里。

所有状态变更均校验当前值（Lua CAS），避免：

- 发送期间 reservation 过期 → 旧 owner 覆盖新 owner 的状态；
- 发送失败协程误删他人 reservation；
- 崩溃残留的 pending 被删除任务或 try_extend 过度续命；
- 窗口已过期时仍写入用户，把孤立数据留给下一个窗口。

参考范式：``src/services/spam_review.py`` 的 Lua CAS。
"""

from __future__ import annotations

import secrets
from typing import Literal, NamedTuple

from src.core.redis import RedisKeys, get_redis

type VerificationHintFlow = Literal["join", "join_request"]

_PENDING_PREFIX = "pending:"
_MESSAGE_ID_PREFIX = "message_id:"


class HintEditClaim(NamedTuple):
    """一次 mention 补全编辑的授权结果（同一窗口内原子取得）。

    Attributes:
        message_id: 待编辑的引导消息 ID
        mention_ids: 本次应渲染的用户（按加入顺序，已截断到上限）
        total: 窗口内等待验证的用户总数（> len(mention_ids) 即发生溢出）
    """

    message_id: int
    mention_ids: list[int]
    total: int


def _pending_value(owner_token: str) -> str:
    return f"{_PENDING_PREFIX}{owner_token}"


# 竞争发送权：SET NX EX 成功即代表开启新窗口，必须在同一脚本里清空上一窗口
# 残留的 users/render——否则上个窗口没来得及过期的用户会被 mention 进新消息，
# 而残留的 render 版本会让新消息的首次编辑被 CAS 拒绝。
# SET NX 失败说明窗口属于他人（pending 或已提交），此时绝不能碰附属键。
#
# KEYS = [hint, users, render]
# ARGV = [pending_value, ttl]
_RESERVE_HINT_SCRIPT = """
if not redis.call("set", KEYS[1], ARGV[1], "NX", "EX", ARGV[2]) then
    return 0
end
redis.call("del", KEYS[2], KEYS[3])
return 1
""".strip()


# 把等待验证的用户加入当前窗口。先校验 hint 键存在：窗口已过期时拒绝写入，
# 避免用户集合先于新窗口建立、被下一条引导消息继承。
# score 取写入前的 ZCARD，成员只增不删（整键随窗口清空），故序号不重复，
# ZRANGE 天然按加入顺序返回；ZADD NX 保证同一用户重复入群不重复占位。
#
# KEYS = [hint, users]
# ARGV = [user_id, ttl]
# 返回 {是否新增, hint 当前值, 用户总数}；窗口不存在返回 {0, "", 0}
_ADD_HINT_USER_SCRIPT = """
local hint = redis.call("get", KEYS[1])
if not hint then
    return {0, "", 0}
end
local added = redis.call("zadd", KEYS[2], "NX", redis.call("zcard", KEYS[2]), ARGV[1])
redis.call("expire", KEYS[2], ARGV[2])
return {added, hint, redis.call("zcard", KEYS[2])}
""".strip()


# 读取用户快照：总数与截断后的成员必须在同一脚本内取，否则并发写入会让
# 「渲染出的 mention 数」与「用于溢出日志的总数」对不上。
#
# KEYS = [users]
# ARGV = [limit]
# 返回 {总数, {前 limit 个 user_id}}
_SNAPSHOT_HINT_USERS_SCRIPT = """
local total = redis.call("zcard", KEYS[1])
local limit = tonumber(ARGV[1]) or 0
if total == 0 or limit <= 0 then
    return {total, {}}
end
return {total, redis.call("zrange", KEYS[1], 0, limit - 1)}
""".strip()


# 编辑权 CAS：只有严格更大的版本（已渲染 mention 数）才能提交，且必须仍指向
# 调用方所认知的那条消息。两个晚到用户各自触发一次编辑时，先发起但后到达的
# 请求会被拒，防止把已含 3 个 mention 的消息覆盖回 2 个；message_id 校验则挡住
# 旧窗口协程写坏新窗口的版本（旧窗口的 hint 值已不同）。
#
# KEYS = [hint, render]
# ARGV = [expected_hint_value, version, ttl]
_CLAIM_HINT_RENDER_SCRIPT = """
if redis.call("get", KEYS[1]) ~= ARGV[1] then
    return 0
end
local current = tonumber(redis.call("get", KEYS[2]) or "0") or 0
if tonumber(ARGV[2]) <= current then
    return 0
end
redis.call("set", KEYS[2], ARGV[2], "EX", ARGV[3])
return 1
""".strip()


# 晚到用户的编辑权：读窗口状态、取用户快照、提交版本必须原子完成。
# 分成多次调用会出现「快照取自旧窗口、message_id 取自新窗口」的交错，把上一批
# 用户 mention 进新消息，同时挤掉新窗口自己的用户。
#
# KEYS = [hint, users, render]
# ARGV = [limit, ttl]
# 返回 {hint 值或 ""（空=不该编辑）, 用户总数, 截断后的 user_id 列表}
_CLAIM_HINT_EDIT_SCRIPT = """
local hint = redis.call("get", KEYS[1])
local limit = tonumber(ARGV[1]) or 0
if not hint or string.sub(hint, 1, 11) ~= "message_id:" or limit <= 0 then
    -- 窗口不存在、消息尚未提交（pending 期间的用户由 owner 的快照带出）或功能关闭
    return {"", 0, {}}
end

local total = redis.call("zcard", KEYS[2])
local version = total
if version > limit then
    version = limit
end
local current = tonumber(redis.call("get", KEYS[3]) or "0") or 0
if version <= current then
    -- 渲染内容不会变化（含溢出上限后的新用户），跳过编辑省下 API 配额
    return {"", total, {}}
end

redis.call("set", KEYS[3], version, "EX", ARGV[2])
return {hint, total, redis.call("zrange", KEYS[2], 0, limit - 1)}
""".strip()


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
# 避免删除任务或并发协程为 pending 过度续命。users/render 必须与 hint 同步
# 续期，否则窗口还在、用户集合先过期，晚到用户会看不到先到者的 mention。
#
# KEYS = [hint, users, render]
# ARGV = [ttl]
_EXTEND_HINT_SCRIPT = """
local raw = redis.call("get", KEYS[1])
if not raw or string.sub(raw, 1, 11) ~= "message_id:" then
    return 0
end
redis.call("expire", KEYS[1], ARGV[1])
redis.call("expire", KEYS[2], ARGV[1])
redis.call("expire", KEYS[3], ARGV[1])
return 1
""".strip()


# 删除任务用：仅当当前值仍等于期望 message_id 时原子返回 TTL（避免 get 与 ttl 之间
# 旧 key 过期、新 reservation 建立，导致旧消息按新 key 的 TTL 被拖延，新旧两条共存）。
# 返回 -1 表示值不匹配（不拖延，立即删）；-2 表示 key 不存在；>0 为剩余 TTL 秒。
_GET_HINT_TTL_IF_MATCH_SCRIPT = """
local raw = redis.call("get", KEYS[1])
if raw ~= ARGV[1] then
    return -1
end
local ttl = redis.call("ttl", KEYS[1])
if ttl < 0 then
    return -2
end
return ttl
""".strip()


async def reserve_hint(
    chat_id: int,
    flow: VerificationHintFlow,
    ttl: int = 30,
) -> str | None:
    """竞争 hint 发送权（``SET NX EX`` + 原子清空上一窗口的 users/render）。

    成功返回随机 owner token；已有 pending 或已提交状态时返回 None。
    """
    redis = get_redis()
    owner_token = secrets.token_hex(16)
    acquired = await redis.eval(
        _RESERVE_HINT_SCRIPT,
        3,
        RedisKeys.verification_hint(chat_id, flow),
        RedisKeys.verification_hint_users(chat_id, flow),
        RedisKeys.verification_hint_render(chat_id, flow),
        _pending_value(owner_token),
        ttl,
    )
    return owner_token if acquired else None


async def add_hint_user(
    chat_id: int,
    flow: VerificationHintFlow,
    user_id: int,
    ttl: int = 30,
) -> tuple[bool, bool, int]:
    """把等待验证的用户登记进当前引导窗口。

    Returns:
        ``(是否新登记, 引导消息是否已发出, 窗口内用户总数)``。窗口不存在（已过期
        或尚未建立）时返回 ``(False, False, 0)``，调用方据此跳过 mention 相关动作。
        「引导消息是否已发出」用于区分：pending 期间登记的用户会被 owner 的快照
        一并发出，已提交后登记的用户则需要编辑消息才能补上。
    """
    redis = get_redis()
    result = await redis.eval(
        _ADD_HINT_USER_SCRIPT,
        2,
        RedisKeys.verification_hint(chat_id, flow),
        RedisKeys.verification_hint_users(chat_id, flow),
        user_id,
        ttl,
    )
    if not isinstance(result, (list, tuple)) or len(result) != 3:
        return False, False, 0
    committed = str(result[1]).startswith(_MESSAGE_ID_PREFIX)
    return bool(int(result[0])), committed, int(result[2])


async def snapshot_hint_users(
    chat_id: int,
    flow: VerificationHintFlow,
    limit: int,
) -> tuple[list[int], int]:
    """读取窗口内待 mention 用户快照。

    Returns:
        ``(按加入顺序截断到 limit 的 user_id 列表, 窗口内用户总数)``。总数用于
        判断是否发生 mention 溢出（超出部分不渲染，仅记日志）。
    """
    redis = get_redis()
    result = await redis.eval(
        _SNAPSHOT_HINT_USERS_SCRIPT,
        1,
        RedisKeys.verification_hint_users(chat_id, flow),
        limit,
    )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        return [], 0
    members = result[1] if isinstance(result[1], (list, tuple)) else ()
    return [int(member) for member in members], int(result[0])


async def claim_hint_render(
    chat_id: int,
    flow: VerificationHintFlow,
    message_id: int,
    version: int,
    ttl: int = 30,
) -> bool:
    """为自己刚发出的引导消息提交初始 mention 渲染版本。

    ``version`` 传随消息发出的 mention 数量。仅当窗口仍指向 ``message_id``、且
    版本严格大于已提交版本时返回 True——前者挡住旧窗口协程写坏新窗口的版本，
    后者保证版本单调递增。
    """
    redis = get_redis()
    claimed = await redis.eval(
        _CLAIM_HINT_RENDER_SCRIPT,
        2,
        RedisKeys.verification_hint(chat_id, flow),
        RedisKeys.verification_hint_render(chat_id, flow),
        f"{_MESSAGE_ID_PREFIX}{message_id}",
        version,
        ttl,
    )
    return bool(claimed)


async def claim_hint_edit(
    chat_id: int,
    flow: VerificationHintFlow,
    limit: int,
    ttl: int = 30,
) -> HintEditClaim | None:
    """原子取得「补全已发出引导消息 mention」的编辑权。

    一次调用完成：校验消息已发出 → 取用户快照 → 提交渲染版本，三者同源于同一
    窗口，避免分多次调用时出现跨窗口交错（旧窗口的用户被写进新窗口的消息）。

    Returns:
        应当编辑时返回 :class:`HintEditClaim`；窗口不存在、消息尚未提交、或渲染
        内容不会变化（含用户数已超上限）时返回 None。
    """
    redis = get_redis()
    result = await redis.eval(
        _CLAIM_HINT_EDIT_SCRIPT,
        3,
        RedisKeys.verification_hint(chat_id, flow),
        RedisKeys.verification_hint_users(chat_id, flow),
        RedisKeys.verification_hint_render(chat_id, flow),
        limit,
        ttl,
    )
    if not isinstance(result, (list, tuple)) or len(result) != 3:
        return None

    raw_hint = str(result[0])
    if not raw_hint.startswith(_MESSAGE_ID_PREFIX):
        return None
    try:
        message_id = int(raw_hint[len(_MESSAGE_ID_PREFIX) :])
    except ValueError:
        return None

    members = result[2] if isinstance(result[2], (list, tuple)) else ()
    return HintEditClaim(
        message_id=message_id,
        mention_ids=[int(member) for member in members],
        total=int(result[1]),
    )


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
    """已提交 message_id 存在时同步延长 hint/users/render 三键 TTL。

    pending 或 key 不存在返回 False。
    """
    redis = get_redis()
    extended = await redis.eval(
        _EXTEND_HINT_SCRIPT,
        3,
        RedisKeys.verification_hint(chat_id, flow),
        RedisKeys.verification_hint_users(chat_id, flow),
        RedisKeys.verification_hint_render(chat_id, flow),
        ttl,
    )
    return bool(extended)


async def get_hint_ttl_if_match(
    chat_id: int,
    flow: VerificationHintFlow,
    message_id: int,
) -> int:
    """仅当 hint 仍指向指定 message_id 时原子返回剩余 TTL。

    - >0：剩余 TTL 秒（调用方应继续等待之后重删）
    - -1：值不匹配或 key 不存在（被新 reservation/新消息替换，或已过期；
      调用方应立即删旧消息，不再拖延）
    """
    redis = get_redis()
    return await redis.eval(
        _GET_HINT_TTL_IF_MATCH_SCRIPT,
        1,
        RedisKeys.verification_hint(chat_id, flow),
        f"message_id:{message_id}",
    )
