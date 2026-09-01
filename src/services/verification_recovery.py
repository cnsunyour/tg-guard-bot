"""验证 challenge delivery/recovery Redis 状态机。

同一用户同一群组同一时刻只能有一个验证会话（不按 flow 分键，否则新旧 deep-link 可
分别取锁覆盖同一答案）。状态机统一初始发送与 /start 恢复两条路径，保证：

- 并发 /start 只有一个能 reserve（``undelivered`` → ``pending`` CAS）；
- timeout 携带 ``session_id``，claim 匹配 session 才处理，并从 ``message`` 状态读真实
  message_id（解决 hint 路径 ``message_id=0`` 关联不到新 UI 的问题）；
- 成功与 timeout 均通过 Lua claim 原子消费主键（成功 ``claim_success``、超时
  ``claim_timeout``），使两条路径互斥、防重复处罚/恢复；成功 claim 直接删除 recovery
  终态（不留 ``success`` 标记），避免阻塞后续新 session 的 reserve；
- 成功/超时/管理员清理后旧状态不复活（commit/promote CAS 失败即放弃，绝不覆盖新会话）。

状态值四态（各段均无冒号，``:`` 安全分隔）：

- ``undelivered:{session}``：私聊发送失败（用户未启动 Bot），可经 /start 恢复
- ``pending:{session}:{revision}:{owner_token}``：某协程取得发送权，UI 未提交
  （revision 区分同一 session 的不同发送尝试；owner_token 兼任 CAS 凭证）
- ``message:{session}:{revision}:{flow}:{message_id}``：UI 已发送，timeout claim 据此
  读 message_id
- ``timeout:{session}``：timeout 已 claim，二次 claim 返回 stale（防重复处罚）

TTL 约定：

- ``pending``：``min(30s, deadline - now)``（覆盖 Telegram send 最坏耗时，崩溃残留自然过期）
- ``message`` / ``undelivered`` / ``timeout`` / 主键 / deadline / type /
  captcha_token：``PEXPIREAT deadline + grace``（grace 让 claim 在原始 deadline 触发时仍能读到）

deadline 由 reserve Lua 用 Redis ``TIME`` 计算，避免应用节点与 Redis 时钟偏差；timeout
任务以 claim 返回的剩余毫秒重排，始终以 Redis deadline 为准。

参考范式：``src/services/verification_hint.py`` + ``src/services/spam_review.py`` 的 Lua CAS。
"""

from __future__ import annotations

import contextlib
import secrets
from dataclasses import dataclass
from typing import Literal

from src.core.redis import RedisKeys, get_redis

type VerificationFlow = Literal["join", "join_request"]

# timeout claim 结果（Lua 返回码 → 状态）
type TimeoutClaimStatus = Literal[
    "claimed",  # 匹配 session 且 deadline 已到，message_id 就绪，可执行处罚
    "wait",  # deadline 未到，按 remaining_ms 重排（恢复延长 deadline 的场景）
    "stale",  # session 不匹配 / 已清理 / 已 claim / 格式损坏，timeout 无事可做
]

VERIFICATION_GRACE_MS = 10_000  # 主键/recovery/deadline 在原始 deadline 后的宽限（供 claim 读取）
RECOVERY_RESERVATION_TTL_MS = 30_000  # pending reservation 最长存活（覆盖 Telegram send 最坏耗时）


def _validate_component(value: str, name: str) -> None:
    """校验状态值各段：非空且不含冒号（保证 ``:`` 分隔安全）。"""
    if not isinstance(value, str) or not value or ":" in value:
        raise ValueError(f"{name} 必须为非空且不含冒号的字符串")


def _as_text(value: object) -> str:
    """规范化 Redis 返回值；正式客户端开启 decode_responses 时已是 str。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    raise TypeError("Redis 返回值不是字符串")


def redis_text(value: object) -> str | None:
    """宽松版 Redis 返回值规范化：脏数据返回 None（由调用方跳过）。

    与 :func:`_as_text` 的区别：不抛异常——适用于启动恢复扫描这类
    「坏一个键跳过一个键」的容错场景（_as_text 用于状态机断言路径）。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        with contextlib.suppress(UnicodeDecodeError):
            return value.decode("utf-8")
    return None


def parse_deadline_value(raw: str) -> tuple[str, int] | None:
    """解析 deadline 值 ``{session}:{deadline_ms}``，非法返回 None。

    deadline 值格式的唯一权威解析入口（写入方为 reserve Lua 与 commit 路径）：
    session 段非空且不含冒号（最后一个 ``:`` 拆分），deadline 段全数字。
    启动恢复扫描与 :func:`get_deadline_ms` 必须经此解析，防止多处手写
    校验逻辑漂移。
    """
    session_id, sep, deadline_text = raw.rpartition(":")
    if not sep or not session_id or ":" in session_id or not deadline_text.isdigit():
        return None
    try:
        return session_id, int(deadline_text)
    except ValueError:
        # isdigit() 对部分 Unicode 数字（如上标 ²）为 True 但 int() 拒绝——脏值按 None 跳过
        return None


@dataclass(frozen=True, slots=True)
class RecoveryReservation:
    """一次 challenge 生成/发送 reservation，贯穿 reserve → commit → promote → release。

    initial 模式 ``expected_state_value=None``（校验主键不存在）；recovery 模式为旧主键
    值（commit CAS 校验未被替换）。``pending_value`` 是 promote/release/commit 的 CAS 凭证。
    """

    chat_id: int
    user_id: int
    session_id: str
    revision: str
    owner_token: str
    deadline_ms: int
    expected_state_value: str | None
    initial: bool

    @property
    def pending_value(self) -> str:
        return f"pending:{self.session_id}:{self.revision}:{self.owner_token}"


@dataclass(frozen=True, slots=True)
class VerificationClearToken:
    """clear CAS 快照；延迟清理路径必须在 Telegram 副作用前取得。

    ``state_value``/``deadline_value``/``recovery_value`` 为捕获时刻的 Redis 值（None=键
    不存在）。clear Lua 校验当前值仍匹配快照才删，防止旧协程在 grace 期后把新 session
    当当前 session 误删。
    """

    state_value: str | None
    deadline_value: str | None
    recovery_value: str | None


@dataclass(frozen=True, slots=True)
class TimeoutClaim:
    """timeout claim 结果。

    ``claimed`` 时 message_id 就绪、``clear_token`` 携带 claim 时快照（ban 网络后 clear 用）；
    ``wait`` 时 remaining_ms 用于重排。
    """

    status: TimeoutClaimStatus
    message_id: int = 0
    remaining_ms: int = 0
    clear_token: VerificationClearToken | None = None


def new_session_id() -> str:
    """生成验证会话 ID（32 位十六进制，作为 timeout/恢复的身份标识）。"""
    return secrets.token_hex(16)


def new_revision_id() -> str:
    """生成恢复 revision（区分同一 session 的不同发送尝试，4b /start 恢复用）。"""
    return secrets.token_hex(8)


# --- Redis Lua 脚本 ---
# 测试用 _FakeRedis 模拟这些脚本的语义；真 Redis 行为待真机验证（3c1-4b 接入后）。

# reserve：initial 校验 recovery/main/deadline 均不存在（SET NX 语义）；recovery 校验
# main/deadline 仍在且 recovery 为空或 undelivered:{原session}（CAS）。deadline 由 Redis
# TIME 计算（避免时钟偏差），reservation TTL = min(deadline, now + 30s)。
# 返回 {1, deadline_ms_str, expected_main(or ""), session}；失败 {0, reason, ""}。
_RESERVE_RECOVERY_SCRIPT = """
local mode = ARGV[1]
local requested_session = ARGV[2]
local revision = ARGV[3]
local owner = ARGV[4]
local timeout_ms = tonumber(ARGV[5])
local reservation_ttl_ms = tonumber(ARGV[6])

local clock = redis.call("time")
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)

local session
local deadline_ms
local expected_main = ""

if mode == "initial" then
    if redis.call("exists", KEYS[1]) == 1
        or redis.call("exists", KEYS[2]) == 1
        or redis.call("exists", KEYS[3]) == 1 then
        return {0, "busy", ""}
    end

    if not timeout_ms or timeout_ms <= 0 then
        return {0, "invalid_timeout", ""}
    end

    session = requested_session
    deadline_ms = now_ms + timeout_ms
else
    local deadline_raw = redis.call("get", KEYS[3])
    local main_raw = redis.call("get", KEYS[2])
    if not deadline_raw or not main_raw then
        return {0, "missing", ""}
    end

    local separator = string.find(deadline_raw, ":", 1, true)
    if not separator then
        return {0, "invalid_deadline", ""}
    end

    session = string.sub(deadline_raw, 1, separator - 1)
    deadline_ms = tonumber(string.sub(deadline_raw, separator + 1))
    if not deadline_ms or deadline_ms <= now_ms then
        return {0, "expired", ""}
    end

    local recovery_raw = redis.call("get", KEYS[1])
    if recovery_raw and recovery_raw ~= "undelivered:" .. session then
        return {0, "busy", ""}
    end

    expected_main = main_raw
end

local pending = "pending:" .. session .. ":" .. revision .. ":" .. owner
local reservation_deadline = math.min(deadline_ms, now_ms + reservation_ttl_ms)
redis.call("set", KEYS[1], pending, "PXAT", reservation_deadline)

return {1, tostring(deadline_ms), expected_main, session}
""".strip()

# commit：校验 recovery == 自己 pending + deadline 未到，原子写主键 + deadline + type +
# captcha_token(auxiliary)，全部 PEXPIREAT deadline+grace。initial 校验主键/deadline 不存在；
# recovery 校验旧主键/deadline/type 未被替换。无 auxiliary 时删旧 token。
# KEYS = [recovery, main, deadline, type, captcha_token]
_COMMIT_RECOVERY_SCRIPT = """
local expected_pending = ARGV[1]
local mode = ARGV[2]
local expected_main = ARGV[3]
local session = ARGV[4]
local deadline_ms = tonumber(ARGV[5])
local state_value = ARGV[6]
local flow = ARGV[7]
local auxiliary_value = ARGV[8]
local grace_ms = tonumber(ARGV[9])

local clock = redis.call("time")
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if not deadline_ms or now_ms >= deadline_ms then
    return 0
end

if redis.call("get", KEYS[1]) ~= expected_pending then
    return 0
end

local deadline_value = session .. ":" .. tostring(deadline_ms)
if mode == "initial" then
    if redis.call("exists", KEYS[2]) == 1
        or redis.call("exists", KEYS[3]) == 1 then
        return 0
    end
else
    if redis.call("get", KEYS[2]) ~= expected_main
        or redis.call("get", KEYS[3]) ~= deadline_value
        or redis.call("get", KEYS[4]) ~= flow then
        return 0
    end
end

local expire_at = deadline_ms + grace_ms

redis.call("set", KEYS[2], state_value)
redis.call("pexpireat", KEYS[2], expire_at)

redis.call("set", KEYS[3], deadline_value)
redis.call("pexpireat", KEYS[3], expire_at)

redis.call("set", KEYS[4], flow)
redis.call("pexpireat", KEYS[4], expire_at)

if auxiliary_value ~= "" then
    redis.call("set", KEYS[5], auxiliary_value)
    redis.call("pexpireat", KEYS[5], expire_at)
else
    redis.call("del", KEYS[5])
end

return 1
""".strip()

# promote：仅 owner 将 pending 提升为 message（记录真实 message_id），重设 TTL=deadline+grace。
# 校验 deadline 未到 + recovery==pending + deadline 未被改 + 主键仍在（防 promote 已 claim 的 session）。
_PROMOTE_RECOVERY_SCRIPT = """
local expected_pending = ARGV[1]
local expected_deadline = ARGV[2]
local message_value = ARGV[3]
local deadline_ms = tonumber(ARGV[4])
local grace_ms = tonumber(ARGV[5])

local clock = redis.call("time")
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)

if now_ms >= deadline_ms
    or redis.call("get", KEYS[1]) ~= expected_pending
    or redis.call("get", KEYS[2]) ~= expected_deadline
    or redis.call("exists", KEYS[3]) == 0 then
    return 0
end

redis.call("set", KEYS[1], message_value, "PXAT", deadline_ms + grace_ms)
return 1
""".strip()

# release：preserve=1（Forbidden）时 pending → undelivered:{session}，保留主键/deadline/type
# 供 /start 恢复；preserve=0（其他发送错误）时按 owner 原子删除整个 session 全部状态键。
_RELEASE_RECOVERY_SCRIPT = """
local expected_pending = ARGV[1]
local undelivered_value = ARGV[2]
local expected_deadline = ARGV[3]
local deadline_ms = tonumber(ARGV[4])
local grace_ms = tonumber(ARGV[5])
local preserve = ARGV[6]

if redis.call("get", KEYS[1]) ~= expected_pending then
    return 0
end

if preserve == "0" then
    redis.call("del", KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5])
    return 1
end

if redis.call("exists", KEYS[2]) == 0
    or redis.call("get", KEYS[3]) ~= expected_deadline then
    return 0
end

redis.call("set", KEYS[1], undelivered_value, "PXAT", deadline_ms + grace_ms)
return 1
""".strip()

# verdict claim：答案裁决（成功或失败）后原子消费 challenge，与 timeout claim 互斥。
# claim_success 与 claim_failure 共用此脚本——两者状态机操作完全一致（双 CAS + 删整个
# session + 返回 flow），差异仅在调用语义（恢复权限 vs 处罚）与 handler 后续动作。
#
# 同时 CAS main 与 deadline：main CAS 防 timeout 已删/改主键；deadline CAS 防新 session
# 恰好生成相同答案的 ABA（旧答案快照不能消费新会话）。expected_main/expected_deadline
# 由调用方一次 MGET 同步读取，保证快照一致。
#
# flow 在 Lua 内校验后随结果原子返回；type 与其他 session 键一并删除，避免 handler
# 在 deadline+grace 边缘再次 GET type 时把 join_request 误判为 join（P2.5）。
#
# KEYS = [recovery, main, deadline, type, captcha_token]
# ARGV = [expected_main, expected_deadline, grace_ms]
# 返回 {1, flow}=已 claim；0=CAS 失败（timeout 接管 / session 切换 / 状态损坏 / grace 已过）。
_CLAIM_VERDICT_SCRIPT = """
local expected_main = ARGV[1]
local expected_deadline = ARGV[2]
local grace_ms = tonumber(ARGV[3])

if not grace_ms or grace_ms < 0 then
    return 0
end

-- deadline CAS（含格式校验：session 非空、deadline_ms 正整数）
local deadline_raw = redis.call("get", KEYS[3])
if not deadline_raw or deadline_raw ~= expected_deadline then
    return 0
end

local separator = string.find(deadline_raw, ":", 1, true)
if not separator or separator == 1 then
    return 0
end
local deadline_ms = tonumber(string.sub(deadline_raw, separator + 1))
if not deadline_ms or deadline_ms <= 0 then
    return 0
end

-- main CAS：答案快照未被 timeout 消费/替换
if redis.call("get", KEYS[2]) ~= expected_main then
    return 0
end

-- flow 白名单（防 type 键脏值）
local flow = redis.call("get", KEYS[4])
if flow ~= "join" and flow ~= "join_request" then
    return 0
end

-- grace 期保护：deadline + grace 已过则不 claim（_FakeRedis 无 TTL 需显式判断；
-- 真 Redis 此时 deadline 键已自然过期，前面 GET 已返回 nil）
local clock = redis.call("time")
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if now_ms >= deadline_ms + grace_ms then
    return 0
end

-- 原子消费：flow 已保存在局部变量，删整个 session（含 type）并返回 flow 供 handler 直接用，
-- 避免 handler 在 deadline+grace 边缘再次 GET type 时把 join_request 误判为 join。
-- deadline/main 删除使后续 claim_timeout 在 GET 阶段即 stale；recovery 删除避免
-- 阻塞新 session 的 reserve。
redis.call("del", KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5])
return {1, flow}
""".strip()

# claim：timeout 到期后判断是否可执行处罚。session 匹配且 deadline 已到时：
# - 从 message 状态提取真实 message_id（undelivered/pending 时为 0）；
# - 删主键 + captcha_token，使 timeout 与用户成功回调互斥；
# - recovery → timeout:{session}，防二次 claim 重复处罚。
# 返回 {0,0,0}=stale（session 不匹配/已清理/已 claim/格式损坏）；{1,0,remaining_ms}=wait；
# {2, message_id, 0}=claimed。
_CLAIM_TIMEOUT_SCRIPT = """
local session = ARGV[1]
local flow = ARGV[2]
local grace_ms = tonumber(ARGV[3])

if flow ~= "join" and flow ~= "join_request" then
    return {0, 0, 0}
end

local deadline_raw = redis.call("get", KEYS[3])
if not deadline_raw then
    return {0, 0, 0}
end

local expected_prefix = session .. ":"
if string.sub(deadline_raw, 1, string.len(expected_prefix)) ~= expected_prefix then
    return {0, 0, 0}
end

local deadline_ms = tonumber(string.sub(deadline_raw, string.len(expected_prefix) + 1))
if not deadline_ms then
    return {0, 0, 0}
end

if redis.call("get", KEYS[4]) ~= flow
    or redis.call("exists", KEYS[2]) == 0 then
    return {0, 0, 0}
end

local clock = redis.call("time")
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if now_ms < deadline_ms then
    return {1, 0, deadline_ms - now_ms}
end

local recovery_raw = redis.call("get", KEYS[1])
local message_id = 0
if recovery_raw then
    local undelivered = "undelivered:" .. session
    local timeout_value = "timeout:" .. session

    if recovery_raw == timeout_value then
        return {0, 0, 0}
    elseif recovery_raw == undelivered then
        message_id = 0
    elseif string.match(recovery_raw, "^pending:([^:]+):[^:]+:[^:]+$") == session then
        message_id = 0
    else
        -- 捕获后比较，避免 session 拼入 Lua pattern（防未来 session 含元字符）；
        -- message_flow ~= flow 校验 recovery 与本次 claim 的 flow 一致
        local message_session, message_flow, message_id_text = string.match(
            recovery_raw, "^message:([^:]+):[^:]+:([^:]+):(%d+)$"
        )
        if not message_session
            or message_session ~= session
            or message_flow ~= flow then
            return {0, 0, 0}
        end
        message_id = tonumber(message_id_text)
        if not message_id then
            return {0, 0, 0}
        end
    end
end

local timeout_value = "timeout:" .. session
redis.call("del", KEYS[2], KEYS[5])
redis.call("set", KEYS[1], timeout_value, "PXAT", deadline_ms + grace_ms)
-- 附带 claim 时快照（main 已删→None；deadline 未变；recovery=timeout），供 ban 网络后 clear CAS
return {2, message_id, 0, deadline_raw, timeout_value}
""".strip()

# clear：按调用方在副作用前取得的状态快照删除整个 verification session。
#
# 校验 main + deadline 仍匹配快照（防 session 切换/timeout claim）；deadline 不存在时额外
# 比较 recovery（覆盖 initial reserve 只写 pending、尚未 commit main/deadline 的窗口）。
# 调用方必须在 Telegram 网络调用前 capture 快照——若 clear 时才读，旧协程会把新 session
# 当期望值误删（P2.1 核心）。
#
# KEYS = [recovery, main, deadline, type, captcha_token]
# ARGV = [state_present, expected_state, deadline_present, expected_deadline,
#         recovery_present, expected_recovery]
# 返回删除键数（0=CAS 失败，未删）。
_CLEAR_VERIFICATION_SCRIPT = """
local state_present = ARGV[1] == "1"
local expected_state = ARGV[2]
local deadline_present = ARGV[3] == "1"
local expected_deadline = ARGV[4]
local recovery_present = ARGV[5] == "1"
local expected_recovery = ARGV[6]

local current_state = redis.call("get", KEYS[2])
if state_present then
    if current_state ~= expected_state then
        return 0
    end
elseif current_state then
    return 0
end

local current_deadline = redis.call("get", KEYS[3])
if deadline_present then
    if current_deadline ~= expected_deadline then
        return 0
    end
elseif current_deadline then
    return 0
end

-- 已 commit 的 session 以 deadline 为身份；nil deadline 时 recovery 是 reservation 身份
if not deadline_present then
    local current_recovery = redis.call("get", KEYS[1])
    if recovery_present then
        if current_recovery ~= expected_recovery then
            return 0
        end
    elseif current_recovery then
        return 0
    end
end

return redis.call("del", KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5])
""".strip()


async def reserve_initial_recovery(
    chat_id: int,
    user_id: int,
    session_id: str,
    timeout_ms: int,
) -> RecoveryReservation | None:
    """初始发送：竞争 delivery 权，deadline 由 Redis TIME 计算。

    inflight 锁已防并发，NX 语义（校验 recovery/main/deadline 均不存在）兜底防残留。
    返回 None 表示存在残留旧会话（调用方应记日志，由旧会话 timeout 兜底）。
    """
    _validate_component(session_id, "session_id")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms 必须大于 0")

    owner_token = secrets.token_hex(16)
    redis = get_redis()
    result = await redis.eval(
        _RESERVE_RECOVERY_SCRIPT,
        3,
        RedisKeys.verification_recovery(chat_id, user_id),
        RedisKeys.verification(chat_id, user_id),
        RedisKeys.verification_deadline(chat_id, user_id),
        "initial",
        session_id,
        "initial",
        owner_token,
        timeout_ms,
        RECOVERY_RESERVATION_TTL_MS,
    )
    if int(result[0]) != 1:
        return None

    return RecoveryReservation(
        chat_id=chat_id,
        user_id=user_id,
        session_id=session_id,
        revision="initial",
        owner_token=owner_token,
        deadline_ms=int(result[1]),
        expected_state_value=None,
        initial=True,
    )


async def reserve_recovery(
    chat_id: int,
    user_id: int,
    revision: str,
) -> RecoveryReservation | None:
    """/start 恢复：``undelivered`` → ``pending`` CAS。

    session 从 Redis deadline 键读取（沿用原 session，保证 timeout 身份一致）；revision 是
    新生成（区分新发送尝试）。并发 /start 只有一个 CAS 成功。4b /start handler 调用。
    """
    _validate_component(revision, "revision")
    owner_token = secrets.token_hex(16)
    redis = get_redis()
    result = await redis.eval(
        _RESERVE_RECOVERY_SCRIPT,
        3,
        RedisKeys.verification_recovery(chat_id, user_id),
        RedisKeys.verification(chat_id, user_id),
        RedisKeys.verification_deadline(chat_id, user_id),
        "recovery",
        "",
        revision,
        owner_token,
        0,
        RECOVERY_RESERVATION_TTL_MS,
    )
    if int(result[0]) != 1:
        return None

    return RecoveryReservation(
        chat_id=chat_id,
        user_id=user_id,
        session_id=_as_text(result[3]),
        revision=revision,
        owner_token=owner_token,
        deadline_ms=int(result[1]),
        expected_state_value=_as_text(result[2]),
        initial=False,
    )


async def commit_recovery(
    reservation: RecoveryReservation,
    *,
    state_value: str,
    auxiliary_state: str | None,
    flow: VerificationFlow,
) -> bool:
    """按 reservation owner/旧主键 CAS 原子提交 challenge 状态。

    失败（recovery 已变或主键被替换）返回 False，调用方应放弃发送。auxillary_state 为
    WebApp captcha_token（同事务提交）；None 时删除旧 token。
    """
    redis = get_redis()
    committed = await redis.eval(
        _COMMIT_RECOVERY_SCRIPT,
        5,
        RedisKeys.verification_recovery(reservation.chat_id, reservation.user_id),
        RedisKeys.verification(reservation.chat_id, reservation.user_id),
        RedisKeys.verification_deadline(reservation.chat_id, reservation.user_id),
        RedisKeys.verification_type(reservation.chat_id, reservation.user_id),
        RedisKeys.captcha_token(reservation.chat_id, reservation.user_id),
        reservation.pending_value,
        "initial" if reservation.initial else "recovery",
        reservation.expected_state_value or "",
        reservation.session_id,
        reservation.deadline_ms,
        state_value,
        flow,
        auxiliary_state or "",
        VERIFICATION_GRACE_MS,
    )
    return bool(committed)


async def promote_recovery(
    reservation: RecoveryReservation,
    flow: VerificationFlow,
    message_id: int,
) -> bool:
    """将自己的 pending 原子提升为 message（记录真实 message_id 供 timeout claim 读取）。

    发送期间 reservation 过期、状态已 clear/claim 或 deadline 已到均返回 False（调用方
    应删除未受状态机管理的 UI）。
    """
    redis = get_redis()
    message_value = f"message:{reservation.session_id}:{reservation.revision}:{flow}:{message_id}"
    promoted = await redis.eval(
        _PROMOTE_RECOVERY_SCRIPT,
        3,
        RedisKeys.verification_recovery(reservation.chat_id, reservation.user_id),
        RedisKeys.verification_deadline(reservation.chat_id, reservation.user_id),
        RedisKeys.verification(reservation.chat_id, reservation.user_id),
        reservation.pending_value,
        f"{reservation.session_id}:{reservation.deadline_ms}",
        message_value,
        reservation.deadline_ms,
        VERIFICATION_GRACE_MS,
    )
    return bool(promoted)


async def release_recovery(
    reservation: RecoveryReservation,
    *,
    preserve_challenge: bool,
) -> bool:
    """发送失败时清理。

    ``preserve_challenge=True``（TelegramForbiddenError）：pending → undelivered，保留
    主键/deadline/type 供 /start 恢复。``preserve_challenge=False``（其他发送错误）：按
    owner 原子删除整个 session 全部状态键（不留不可恢复状态）。
    """
    redis = get_redis()
    released = await redis.eval(
        _RELEASE_RECOVERY_SCRIPT,
        5,
        RedisKeys.verification_recovery(reservation.chat_id, reservation.user_id),
        RedisKeys.verification(reservation.chat_id, reservation.user_id),
        RedisKeys.verification_deadline(reservation.chat_id, reservation.user_id),
        RedisKeys.verification_type(reservation.chat_id, reservation.user_id),
        RedisKeys.captcha_token(reservation.chat_id, reservation.user_id),
        reservation.pending_value,
        f"undelivered:{reservation.session_id}",
        f"{reservation.session_id}:{reservation.deadline_ms}",
        reservation.deadline_ms,
        VERIFICATION_GRACE_MS,
        "1" if preserve_challenge else "0",
    )
    return bool(released)


async def _claim_verdict(
    chat_id: int,
    user_id: int,
    expected_state_value: str,
    expected_deadline_value: str,
) -> VerificationFlow | None:
    """答案裁决（成功/失败）原子 claim，与 timeout 互斥。

    claim_success 与 claim_failure 共用此实现：双 CAS（main + deadline，防 ABA）+ flow 白名单
    + grace 期，原子删 recovery/main/deadline/type/captcha_token，返回已校验的 flow。两条
    verdict 路径与 timeout 三方互斥——任一先 claim 即删 main，其余在 GET 阶段 stale。

    None 表示 timeout 已接管、session 已切换、状态损坏或 grace 期已过。调用方必须
    视为 expired：不恢复权限，也不执行失败处罚（两条路径原子互斥，对方已处理）。
    """
    redis = get_redis()
    result = await redis.eval(
        _CLAIM_VERDICT_SCRIPT,
        5,
        RedisKeys.verification_recovery(chat_id, user_id),
        RedisKeys.verification(chat_id, user_id),
        RedisKeys.verification_deadline(chat_id, user_id),
        RedisKeys.verification_type(chat_id, user_id),
        RedisKeys.captcha_token(chat_id, user_id),
        expected_state_value,
        expected_deadline_value,
        VERIFICATION_GRACE_MS,
    )
    if result == 0:
        return None
    if not isinstance(result, list) or len(result) != 2 or int(result[0]) != 1:
        raise RuntimeError("claim verdict Lua 返回格式错误")

    flow = _as_text(result[1])
    if flow == "join":
        return "join"
    if flow == "join_request":
        return "join_request"

    # Lua 已做白名单校验；协议漂移必须 fail closed。
    raise RuntimeError("claim verdict Lua 返回了非法 flow")


async def claim_success(
    chat_id: int,
    user_id: int,
    expected_state_value: str,
    expected_deadline_value: str,
) -> VerificationFlow | None:
    """答案正确后原子 claim 成功路径（语义包装 ``_claim_verdict``）。

    成功时 handler 据返回的 flow 恢复权限/批准加入；None 表示已过期，静默退出。
    """
    return await _claim_verdict(chat_id, user_id, expected_state_value, expected_deadline_value)


async def claim_failure(
    chat_id: int,
    user_id: int,
    expected_state_value: str,
    expected_deadline_value: str,
) -> VerificationFlow | None:
    """答案错误后原子 claim 失败路径（语义包装 ``_claim_verdict``）。

    先删 main 使后续 claim_timeout 在 GET 阶段即 stale，消除 ban/decline 经网络卡入 grace
    期时 timeout 与 handler 重复处罚。handler 据返回的 flow 决定 decline（join_request）或
    ban（join）；None 表示 timeout 已接管，调用方视为 expired 静默退出，不处罚。
    """
    return await _claim_verdict(chat_id, user_id, expected_state_value, expected_deadline_value)


async def capture_verification_clear_token(
    chat_id: int,
    user_id: int,
) -> VerificationClearToken:
    """原子读取 clear CAS 快照。

    延迟清理路径（处罚后/成功后）必须在 Telegram 副作用前调用，避免旧协程在 grace 期后
    把新 session 当当前 session 误删。
    """
    redis = get_redis()
    state_raw, deadline_raw, recovery_raw = await redis.mget(
        RedisKeys.verification(chat_id, user_id),
        RedisKeys.verification_deadline(chat_id, user_id),
        RedisKeys.verification_recovery(chat_id, user_id),
    )
    return VerificationClearToken(
        state_value=_as_text(state_raw) if state_raw is not None else None,
        deadline_value=_as_text(deadline_raw) if deadline_raw is not None else None,
        recovery_value=_as_text(recovery_raw) if recovery_raw is not None else None,
    )


async def clear_verification_state(
    chat_id: int,
    user_id: int,
    expected: VerificationClearToken,
) -> bool:
    """仅当当前 session 仍匹配 expected 快照时删除全部状态键。

    main + deadline CAS 防 session 切换/timeout claim；deadline 不存在时比较 recovery
    （保护 initial reserve 写 pending 未 commit 的窗口）。返回 False 表示状态已变（新
    session/已 claim/已清理），调用方应放弃后续操作。
    """
    redis = get_redis()
    deleted = await redis.eval(
        _CLEAR_VERIFICATION_SCRIPT,
        5,
        RedisKeys.verification_recovery(chat_id, user_id),
        RedisKeys.verification(chat_id, user_id),
        RedisKeys.verification_deadline(chat_id, user_id),
        RedisKeys.verification_type(chat_id, user_id),
        RedisKeys.captcha_token(chat_id, user_id),
        "1" if expected.state_value is not None else "0",
        expected.state_value or "",
        "1" if expected.deadline_value is not None else "0",
        expected.deadline_value or "",
        "1" if expected.recovery_value is not None else "0",
        expected.recovery_value or "",
    )
    return bool(deleted)


async def claim_timeout(
    chat_id: int,
    user_id: int,
    session_id: str,
    flow: VerificationFlow,
) -> TimeoutClaim:
    """按 session 原子 claim timeout，返回当前真实 message_id。

    - ``claimed``：session 匹配且 deadline 已到，主键/token 已删、recovery=timeout，可处罚
    - ``wait``：deadline 未到，按 ``remaining_ms`` 重排（恢复延长了 deadline）
    - ``stale``：session 不匹配 / 已清理 / 已 claim / 格式损坏，timeout 退出
    """
    _validate_component(session_id, "session_id")
    redis = get_redis()
    result = await redis.eval(
        _CLAIM_TIMEOUT_SCRIPT,
        5,
        RedisKeys.verification_recovery(chat_id, user_id),
        RedisKeys.verification(chat_id, user_id),
        RedisKeys.verification_deadline(chat_id, user_id),
        RedisKeys.verification_type(chat_id, user_id),
        RedisKeys.captcha_token(chat_id, user_id),
        session_id,
        flow,
        VERIFICATION_GRACE_MS,
    )

    code = int(result[0])
    if code == 1:
        return TimeoutClaim(status="wait", remaining_ms=max(0, int(result[2])))
    if code == 2:
        return TimeoutClaim(
            status="claimed",
            message_id=max(0, int(result[1])),
            clear_token=VerificationClearToken(
                state_value=None,  # main 已被 claim 删除
                deadline_value=_as_text(result[3]),
                recovery_value=_as_text(result[4]),
            ),
        )
    return TimeoutClaim(status="stale")


async def get_deadline_ms(chat_id: int, user_id: int) -> int | None:
    """读取原始 deadline epoch ms（/start 恢复据此算剩余时间）。返回 None 表示无记录。"""
    redis = get_redis()
    raw = await redis.get(RedisKeys.verification_deadline(chat_id, user_id))
    if raw is None:
        return None
    parsed = parse_deadline_value(raw)
    return parsed[1] if parsed else None
