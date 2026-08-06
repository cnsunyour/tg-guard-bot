"""垃圾消息人工复核状态基础设施。

领域模型 ``SpamReviewState`` 是单条垃圾消息的不可变复核快照：检测命中时写入 Redis，
管理员通过 ``spam_review`` callback 确认（ban / false_positive）后再消费。业务数据
（offender / 原文 / OCR / reason_codes / confidence）只在本层流转；**展示文案与
keyboard 由 antispam_render 按 locale 渲染**，从而支持 i18n 且不把翻译卷入业务判断。

并发模型：

- ``create_review_state`` 用 ``SET NX EX``，同一原消息重复 update / 编辑再次命中时
  保留首份快照，不覆盖、不重复发提示；
- ``consume_review_state`` / ``delete_review_state_if_match`` 用 Lua ``cjson.decode``
  在 Redis 内按 ``review_id`` 原子消费，避免 GET-then-DEL 竞态；
- ``review_lock`` 为 callback 处理窗口提供互斥（owner token + Lua CAS 释放），
  TTL 仅作死锁兜底，应覆盖 Telegram 429 重试 + 封禁 + 数据库写入最坏耗时。

参考范式：``src/bot/handlers/verification.py`` 的 ``_verification_inflight_lock``。
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, NoReturn

from src.core.redis import RedisKeys, get_redis

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class SpamMessageType(StrEnum):
    """支持人工复核的原始消息类型（稳定 code，renderer 按 locale 映射展示文本）。"""

    text = "text"
    photo = "photo"
    sticker = "sticker"
    edited_text = "edited_text"
    edited_photo = "edited_photo"


# review_id 格式：secrets.token_hex(8) 生成 16 位小写十六进制；反序列化宽容大小写
_REVIEW_ID_PATTERN = re.compile(r"[0-9a-fA-F]{16}")

# to_json / from_json 严格匹配的字段集合（拒绝缺字段 / 多未知字段）
_SERIALIZED_FIELDS = frozenset(
    {
        "schema_version",
        "review_id",
        "offender_user_id",
        "message_type",
        "original_text",
        "recognized_text",
        "sample_text",
        "reason_codes",
        "confidence",
    }
)


def _new_review_id() -> str:
    """生成 16 位十六进制 review_id。"""
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


def _require_json_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须为字符串")
    return value


def _redis_value_to_text(value: object) -> str:
    """规范化 Redis 返回值；正式客户端开启 decode_responses 时已是 str。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    raise TypeError("Redis 状态值不是字符串")


@dataclass(frozen=True, slots=True, kw_only=True)
class SpamReviewState:
    """单条垃圾消息的不可变复核快照。

    所有文本字段（original_text / recognized_text / sample_text / reason_codes）
    保留**原始值**，不在本层 HTML escape —— escape 由展示层在插入 Telegram 消息前完成。
    """

    schema_version: Literal["v1"] = "v1"
    review_id: str = field(default_factory=_new_review_id)
    offender_user_id: int
    message_type: SpamMessageType
    original_text: str
    recognized_text: str | None
    sample_text: str
    reason_codes: tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        """新建与反序列化共用同一组不变量校验（单一校验点）。"""
        if self.schema_version != "v1":
            raise ValueError("schema_version 必须为 v1")

        if not isinstance(self.review_id, str) or not _REVIEW_ID_PATTERN.fullmatch(self.review_id):
            raise ValueError("review_id 必须为 16 位十六进制字符串")

        # bool 是 int 子类，需单独排除以免 True/False 被当作 user_id / confidence
        if isinstance(self.offender_user_id, bool) or not isinstance(self.offender_user_id, int):
            raise TypeError("offender_user_id 必须为 int")

        if not isinstance(self.message_type, SpamMessageType):
            raise TypeError("message_type 必须为 SpamMessageType")

        if not isinstance(self.original_text, str):
            raise TypeError("original_text 必须为 str")

        if self.recognized_text is not None and not isinstance(self.recognized_text, str):
            raise TypeError("recognized_text 必须为 str 或 None")

        if not isinstance(self.sample_text, str):
            raise TypeError("sample_text 必须为 str")

        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(code, str) for code in self.reason_codes
        ):
            raise TypeError("reason_codes 必须为仅含 str 的 tuple")

        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("confidence 必须为数字")

        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence 必须为 0 到 1 之间的有限数值")

        # frozen dataclass 改字段须绕过 __setattr__；将 int(0/1) 规范化为 float
        object.__setattr__(self, "confidence", confidence)

    def to_json(self) -> str:
        """序列化为严格、紧凑、保留 Unicode 的 v1 JSON。"""
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "review_id": self.review_id,
                "offender_user_id": self.offender_user_id,
                "message_type": self.message_type.value,
                "original_text": self.original_text,
                "recognized_text": self.recognized_text,
                "sample_text": self.sample_text,
                "reason_codes": list(self.reason_codes),
                "confidence": self.confidence,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> SpamReviewState:
        """严格反序列化 v1 JSON。

        仅做 JSON 结构拆解 + enum / tuple 转换；其余业务不变量（类型、范围、格式）
        统一由 ``__post_init__`` 校验，避免双重校验的维护负担。
        """
        if not isinstance(raw, str):
            raise TypeError("raw 必须为 str")

        try:
            payload = json.loads(
                raw,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_object_from_pairs,
            )
        except ValueError as exc:
            raise ValueError("SpamReviewState JSON 非法") from exc

        if not isinstance(payload, dict):
            raise ValueError("SpamReviewState JSON 顶层必须为 object")

        actual_fields = set(payload)
        if actual_fields != _SERIALIZED_FIELDS:
            missing = sorted(_SERIALIZED_FIELDS - actual_fields)
            unknown = sorted(actual_fields - _SERIALIZED_FIELDS)
            raise ValueError(f"SpamReviewState 字段不匹配: missing={missing}, unknown={unknown}")

        # message_type 需转 enum；reason_codes 需转 tuple；recognized_text 需处理 null。
        # 其余字段原样传入，类型 / 范围由 __post_init__ 统一校验。
        message_type_value = _require_json_string(payload["message_type"], "message_type")
        try:
            message_type = SpamMessageType(message_type_value)
        except ValueError as exc:
            raise ValueError("message_type 非法") from exc

        recognized_text = payload["recognized_text"]
        if recognized_text is not None and not isinstance(recognized_text, str):
            raise ValueError("recognized_text 必须为字符串或 null")

        reason_values = payload["reason_codes"]
        if not isinstance(reason_values, list):
            raise ValueError("reason_codes 必须为 JSON array")
        reason_codes = tuple(_require_json_string(code, "reason_codes[]") for code in reason_values)

        return cls(
            schema_version=payload["schema_version"],
            review_id=payload["review_id"],
            offender_user_id=payload["offender_user_id"],
            message_type=message_type,
            original_text=payload["original_text"],
            recognized_text=recognized_text,
            sample_text=payload["sample_text"],
            reason_codes=reason_codes,
            confidence=payload["confidence"],
        )


# --- Redis Lua 脚本 ---
# 测试用 _FakeRedis 模拟这些脚本的语义；真 Redis 行为待真机验证（3b-3 接入后）。

# 按 review_id 原子消费：匹配则 DEL，始终返回删除前的原始 JSON（不存在返回 false）。
# 调用方据返回值判断：None=不存在；review_id 匹配的 state=已消费；review_id 不匹配的
# state=被他人重建（未删除）。
_CONSUME_REVIEW_SCRIPT = """
local raw = redis.call("get", KEYS[1])
if not raw then
    return false
end

local ok, state = pcall(cjson.decode, raw)
if ok and type(state) == "table" and state["review_id"] == ARGV[1] then
    redis.call("del", KEYS[1])
end

return raw
""".strip()

# 仅当 review_id 匹配时删除（发送提示失败时清理刚写入的 state）。返回是否删除。
_DELETE_REVIEW_IF_MATCH_SCRIPT = """
local raw = redis.call("get", KEYS[1])
if not raw then
    return 0
end

local ok, state = pcall(cjson.decode, raw)
if ok and type(state) == "table" and state["review_id"] == ARGV[1] then
    return redis.call("del", KEYS[1])
end

return 0
""".strip()

# 锁释放：仅当键值等于 owner token 时才删除（与 verification inflight 锁一致）。
_RELEASE_REVIEW_LOCK_SCRIPT = (
    'if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) end return 0'
)


async def create_review_state(
    state: SpamReviewState,
    chat_id: int,
    orig_msg_id: int,
    ttl: int = 86400,
) -> SpamReviewState | None:
    """以 ``SET NX EX`` 写入首份复核快照。

    键已存在（同消息重复 update / 编辑再次命中）时返回 None，保证不覆盖首快照、不重复发提示。
    """
    redis = get_redis()
    created = bool(
        await redis.set(
            RedisKeys.spam_review(chat_id, orig_msg_id),
            state.to_json(),
            nx=True,
            ex=ttl,
        )
    )
    return state if created else None


async def get_review_state(
    chat_id: int,
    orig_msg_id: int,
) -> SpamReviewState | None:
    """读取复核状态；不存在或 JSON 非法时返回 None。"""
    redis = get_redis()
    raw = await redis.get(RedisKeys.spam_review(chat_id, orig_msg_id))
    if raw is None:
        return None
    try:
        return SpamReviewState.from_json(_redis_value_to_text(raw))
    except (TypeError, ValueError):
        return None


async def consume_review_state(
    chat_id: int,
    orig_msg_id: int,
    review_id: str,
) -> SpamReviewState | None:
    """按 review_id 原子消费状态。

    - review_id 匹配：Lua 删除键并返回该状态；
    - review_id 不匹配：保留键，返回当前状态（供调用方识别"已被他人重建"）；
    - 键不存在：返回 None。
    """
    redis = get_redis()
    raw = await redis.eval(
        _CONSUME_REVIEW_SCRIPT,
        1,
        RedisKeys.spam_review(chat_id, orig_msg_id),
        review_id,
    )
    if raw is None or raw is False:
        return None
    try:
        return SpamReviewState.from_json(_redis_value_to_text(raw))
    except (TypeError, ValueError):
        return None


async def delete_review_state_if_match(
    chat_id: int,
    orig_msg_id: int,
    review_id: str,
) -> bool:
    """仅在 review_id 仍匹配首份快照时原子删除（提示发送失败时清理）。"""
    redis = get_redis()
    deleted = await redis.eval(
        _DELETE_REVIEW_IF_MATCH_SCRIPT,
        1,
        RedisKeys.spam_review(chat_id, orig_msg_id),
        review_id,
    )
    return bool(deleted)


@contextlib.asynccontextmanager
async def review_lock(
    chat_id: int,
    orig_msg_id: int,
    ttl: int = 300,
) -> AsyncIterator[bool]:
    """获取带 owner token 校验的复核处理锁。

    用 ``SET NX EX`` 取锁，随机 token 为值；离开上下文时用 Lua compare-and-delete
    释放，确保只删除自己持有的锁。TTL 仅作处理耗时超过锁寿命时的死锁兜底，正常路径
    yield 结束即释放。

    Yields:
        是否取得锁；``False`` 表示已有 callback 在处理，调用方应给"正在处理"提示后返回。
    """
    redis = get_redis()
    lock_key = RedisKeys.spam_review_lock(chat_id, orig_msg_id)
    lock_token = secrets.token_hex(16)
    acquired = bool(await redis.set(lock_key, lock_token, nx=True, ex=ttl))
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await redis.eval(_RELEASE_REVIEW_LOCK_SCRIPT, 1, lock_key, lock_token)
