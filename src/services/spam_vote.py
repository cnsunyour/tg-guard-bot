"""群成员集体投票会话基础设施。

领域模型 ``SpamVoteSession`` 是单条待确认消息（自动检测命中确认模式，或被普通成员
/spam 举报）的投票会话快照。成员经按钮或 /spam、/unspam 指令投票（+1/-1），单向票数
达到 ``threshold`` 后由 handler 在 ``review_lock`` 互斥下原子消费会话并执行处置。

存储布局（单 HASH 键，见 ``RedisKeys.spam_vote``）：

- ``_meta`` → ``SpamVoteSession`` v1 JSON（严格序列化，范式同 ``SpamReviewState``）；
- ``u<user_id>`` → ``"1"`` / ``"-1"``，一人一字段即一人一票，方向固定不可改；
- ``_up`` / ``_down`` → 分向计数（Lua ``HINCRBY`` 维护，进度读取 O(1)）；
- ``_prompt_id`` / ``_prompt_base`` → 提示消息定位与进度重建文案，发送后补写。

并发模型：

- 全部字段同居一个 HASH、共享一个 TTL：票数与元数据同生共死，不存在多键过期错位；
- ``_VOTE_SCRIPT`` 以 ``HGET _meta`` 判存在（Lua 执行期间 Redis 不做惰性过期），
  封死「对已过期键写入导致无 TTL 复活」的经典陷阱；投票不续期（固定窗口）；
- ``_CONSUME_VOTE_SCRIPT`` 按 ``vote_id`` get-match-del 原子消费，同一会话全局至多
  消费一次——这是「投票达阈值 / 管理员直达 / 互斥终局」的唯一裁决点（at-most-once，
  与 spam_review 的消费语义一致）。

参考范式：``src/services/spam_review.py``。
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, NoReturn, cast

from src.core.redis import RedisKeys, get_redis
from src.services.verification_recovery import as_text

# vote_id 格式：secrets.token_hex(8) 生成 16 位小写十六进制；反序列化宽容大小写
_VOTE_ID_PATTERN = re.compile(r"[0-9a-fA-F]{16}")

# to_json / from_json 严格匹配的字段集合（拒绝缺字段 / 多未知字段）
_SERIALIZED_FIELDS = frozenset(
    {
        "schema_version",
        "vote_id",
        "source",
        "offender_user_id",
        "report_id",
        "threshold",
        "sample_text",
    }
)


class SpamVoteSource(StrEnum):
    """投票会话的来源链路（仅溯源与展示，不影响终态动作）。"""

    review = "review"  # 自动检测命中确认模式
    report = "report"  # 普通成员 /spam 举报


def _new_vote_id() -> str:
    """生成 16 位十六进制 vote_id。"""
    return secrets.token_hex(8)


def _reject_json_constant(constant: str) -> NoReturn:
    """拒绝 Python json 默认接受的 NaN / Infinity 扩展值（json.loads 的 parse 钩子）。"""
    raise ValueError(f"非标准 JSON 数值: {constant}")


def _strict_object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构建 JSON object 并拒绝重复字段（json.loads 的 object_pairs_hook 钩子）。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class SpamVoteSession:
    """单条待确认消息的不可变投票会话快照。

    ``threshold`` 在创建时从 ``settings.spam_vote_threshold`` 快照：运行期改配置
    不影响在途会话的判定含义。``sample_text`` 保留训练反馈所需的原文快照——
    终态执行不依赖 spam_review state 的存活（两会话键创建有先后、过期有毫秒级错位）。
    """

    schema_version: Literal["v1"] = "v1"
    vote_id: str = field(default_factory=_new_vote_id)
    source: SpamVoteSource
    offender_user_id: int
    report_id: int | None
    threshold: int
    sample_text: str

    def __post_init__(self) -> None:
        """新建与反序列化共用同一组不变量校验（单一校验点）。"""
        if self.schema_version != "v1":
            raise ValueError("schema_version 必须为 v1")

        if not isinstance(self.vote_id, str) or _VOTE_ID_PATTERN.fullmatch(self.vote_id) is None:
            raise ValueError("vote_id 必须为 16 位十六进制字符串")

        # source 的 enum 转换由 from_json 完成（构造入口为静态类型），无需重复校验

        # bool 是 int 子类，需单独排除以免 True/False 被当作 user_id
        if isinstance(self.offender_user_id, bool) or not isinstance(self.offender_user_id, int):
            raise TypeError("offender_user_id 必须为 int")

        if self.report_id is not None and (
            isinstance(self.report_id, bool) or not isinstance(self.report_id, int)
        ):
            raise TypeError("report_id 必须为 int 或 None")

        if isinstance(self.threshold, bool) or not isinstance(self.threshold, int):
            raise TypeError("threshold 必须为 int")
        if self.threshold < 2:
            raise ValueError("threshold 必须大于等于 2")

        if not isinstance(self.sample_text, str):
            raise TypeError("sample_text 必须为 str")

    def to_json(self) -> str:
        """序列化为严格、紧凑、保留 Unicode 的 v1 JSON。"""
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "vote_id": self.vote_id,
                "source": self.source.value,
                "offender_user_id": self.offender_user_id,
                "report_id": self.report_id,
                "threshold": self.threshold,
                "sample_text": self.sample_text,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> SpamVoteSession:
        """严格反序列化 v1 JSON（结构拆解 + enum 转换，不变量统一交 __post_init__）。"""
        if not isinstance(raw, str):
            raise TypeError("raw 必须为 str")

        try:
            payload = json.loads(
                raw,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_object_from_pairs,
            )
        except ValueError as exc:
            raise ValueError("SpamVoteSession JSON 非法") from exc

        if not isinstance(payload, dict):
            raise ValueError("SpamVoteSession JSON 顶层必须为 object")

        actual_fields = set(payload)
        if actual_fields != _SERIALIZED_FIELDS:
            missing = sorted(_SERIALIZED_FIELDS - actual_fields)
            unknown = sorted(actual_fields - _SERIALIZED_FIELDS)
            raise ValueError(f"SpamVoteSession 字段不匹配: missing={missing}, unknown={unknown}")

        source_value = payload["source"]
        if not isinstance(source_value, str):
            raise ValueError("source 必须为字符串")
        try:
            source = SpamVoteSource(source_value)
        except ValueError as exc:
            raise ValueError("source 非法") from exc

        return cls(
            schema_version=payload["schema_version"],
            vote_id=payload["vote_id"],
            source=source,
            offender_user_id=payload["offender_user_id"],
            report_id=payload["report_id"],
            threshold=payload["threshold"],
            sample_text=payload["sample_text"],
        )


@dataclass(frozen=True, slots=True)
class VoteOutcome:
    """一次投票请求的结果：状态 + 双向最新票数（进度展示与阈值判定共用）。"""

    status: Literal["voted", "already", "no_session", "mismatch"]
    up: int
    down: int


# --- Redis Lua 脚本（测试用 _FakeRedis 按常量分支模拟语义）---

# 创建会话：EXISTS 等价 SET NX（同消息重复检测/举报不覆盖首会话），创建与 EXPIRE
# 原子完成，不存在「创建了但 TTL 未设」的中间态。
_CREATE_VOTE_SCRIPT = """
if redis.call("exists", KEYS[1]) == 1 then
    return 0
end
redis.call("hset", KEYS[1], "_meta", ARGV[1])
redis.call("expire", KEYS[1], tonumber(ARGV[2]))
return 1
""".strip()

# 投票：会话身份校验 → 一人一票（HEXISTS 先行，方向固定不可改）→ 分向计数。
# 返回 {status, up, down}：1=已计入 / 0=已投过 / -2=无会话 / -3=vote_id 不匹配。
_VOTE_SCRIPT = """
local meta = redis.call("hget", KEYS[1], "_meta")
if not meta then
    return {-2, 0, 0}
end

if ARGV[3] ~= "" then
    local ok, session = pcall(cjson.decode, meta)
    if (not ok) or type(session) ~= "table" or session["vote_id"] ~= ARGV[3] then
        return {-3, 0, 0}
    end
end

local up = tonumber(redis.call("hget", KEYS[1], "_up") or "0")
local down = tonumber(redis.call("hget", KEYS[1], "_down") or "0")

local user_field = "u" .. ARGV[1]
if redis.call("hexists", KEYS[1], user_field) == 1 then
    return {0, up, down}
end

if ARGV[2] == "up" then
    redis.call("hset", KEYS[1], user_field, "1")
    up = redis.call("hincrby", KEYS[1], "_up", 1)
else
    redis.call("hset", KEYS[1], user_field, "-1")
    down = redis.call("hincrby", KEYS[1], "_down", 1)
end
return {1, up, down}
""".strip()

# 补写提示消息定位：EXISTS 守卫防止对已过期/已消费会话复活写入；不触碰 TTL。
_RECORD_PROMPT_SCRIPT = """
if redis.call("exists", KEYS[1]) == 0 then
    return 0
end
redis.call("hset", KEYS[1], "_prompt_id", ARGV[1], "_prompt_base", ARGV[2])
return 1
""".strip()

# 按 vote_id 原子消费：匹配则 DEL，始终返回删除前的 meta（不存在返回 false）。
# 调用方据返回值判断：None=不存在；匹配的会话=已消费；不匹配=被重建（未删除）。
_CONSUME_VOTE_SCRIPT = """
local raw = redis.call("hget", KEYS[1], "_meta")
if not raw then
    return false
end

local ok, session = pcall(cjson.decode, raw)
if ok and type(session) == "table" and session["vote_id"] == ARGV[1] then
    redis.call("del", KEYS[1])
end

return raw
""".strip()

# 状态码 → VoteOutcome.status 的映射（与 _VOTE_SCRIPT 返回值一一对应）
_VOTE_STATUS_CODES = {1: "voted", 0: "already", -2: "no_session", -3: "mismatch"}


async def create_vote_session(
    session: SpamVoteSession,
    chat_id: int,
    orig_msg_id: int,
    ttl: int,
) -> SpamVoteSession | None:
    """原子创建投票会话（EXISTS 守卫 + EXPIRE 一步完成）。

    键已存在（同消息重复检测命中 / 重复举报 / 编辑再次触发）时返回 None，
    保证不覆盖首会话、不重复发提示。
    """
    created = await get_redis().eval(
        _CREATE_VOTE_SCRIPT,
        1,
        RedisKeys.spam_vote(chat_id, orig_msg_id),
        session.to_json(),
        ttl,
    )
    return session if bool(created) else None


async def get_vote_session(chat_id: int, orig_msg_id: int) -> SpamVoteSession | None:
    """读取投票会话；不存在或 JSON 非法时返回 None。"""
    raw = await get_redis().hget(RedisKeys.spam_vote(chat_id, orig_msg_id), "_meta")
    if raw is None:
        return None
    try:
        return SpamVoteSession.from_json(as_text(raw))
    except (TypeError, ValueError):
        return None


async def record_vote_prompt(
    chat_id: int,
    orig_msg_id: int,
    prompt_message_id: int,
    prompt_base: str,
) -> bool:
    """发送提示消息后补写定位信息（消息 ID + 不含进度行的基础文案）。

    后续投票进度更新据此重建提示内容；会话已过期/被消费时拒绝写入（防复活，
    EXISTS 守卫即足够——调用方总是刚持有当前会话的创建/读取结果）。
    """
    recorded = await get_redis().eval(
        _RECORD_PROMPT_SCRIPT,
        1,
        RedisKeys.spam_vote(chat_id, orig_msg_id),
        str(prompt_message_id),
        prompt_base,
    )
    return bool(recorded)


async def get_vote_prompt(chat_id: int, orig_msg_id: int) -> tuple[int, str] | None:
    """读取提示消息定位；未记录或字段残缺时返回 None。"""
    values = await get_redis().hmget(
        RedisKeys.spam_vote(chat_id, orig_msg_id),
        "_prompt_id",
        "_prompt_base",
    )
    if not values or values[0] is None or values[1] is None:
        return None
    try:
        return int(as_text(values[0])), as_text(values[1])
    except (TypeError, ValueError):
        return None


async def cast_vote(
    chat_id: int,
    orig_msg_id: int,
    user_id: int,
    direction: Literal["up", "down"],
    *,
    expected_vote_id: str | None = None,
) -> VoteOutcome:
    """原子投出一票：一人一票、方向固定，返回双向最新计数。

    ``expected_vote_id`` 提供时校验会话身份（防旧按钮/旧指令消费被重建的新会话）；
    按钮路径必传，指令路径先 get_vote_session 再携带会话当前的 vote_id。
    """
    result = await get_redis().eval(
        _VOTE_SCRIPT,
        1,
        RedisKeys.spam_vote(chat_id, orig_msg_id),
        str(user_id),
        direction,
        expected_vote_id or "",
    )
    status_code, up, down = (int(item) for item in result)
    status = cast(
        "Literal['voted', 'already', 'no_session', 'mismatch']",
        _VOTE_STATUS_CODES.get(status_code, "no_session"),
    )
    return VoteOutcome(status=status, up=up, down=down)


async def consume_vote_session(
    chat_id: int,
    orig_msg_id: int,
    vote_id: str,
) -> SpamVoteSession | None:
    """按 vote_id 原子消费会话（终局裁决点，全局至多成功一次）。

    - vote_id 匹配：Lua 删除键并返回该会话；
    - vote_id 不匹配：保留键，返回当前会话（供调用方识别"已被重建"）；
    - 键不存在：返回 None。
    """
    raw = await get_redis().eval(
        _CONSUME_VOTE_SCRIPT,
        1,
        RedisKeys.spam_vote(chat_id, orig_msg_id),
        vote_id,
    )
    if raw is None or raw is False:
        return None
    try:
        return SpamVoteSession.from_json(as_text(raw))
    except (TypeError, ValueError):
        return None


async def discard_vote_session(chat_id: int, orig_msg_id: int) -> None:
    """管理员终局处置后无条件关闭该消息的投票（不校验 vote_id）。

    同一 orig_msg_id 至多存在一个会话（create 的 EXISTS 守卫），管理员已对消息
    做出终局表态（含 ignore）即应终止成员投票，无需按来源过滤；键不存在时静默。
    """
    await get_redis().delete(RedisKeys.spam_vote(chat_id, orig_msg_id))
