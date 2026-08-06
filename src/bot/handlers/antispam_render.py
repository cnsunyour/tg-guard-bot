"""反垃圾提示纯渲染层。

将 ``SpamReviewState``（确认模式）或即时检测字段（立即处罚模式）按 locale 渲染为
Telegram HTML 文本与内联键盘。本模块是纯展示层：

- 不注册 aiogram handler、不访问 Redis、不决定检测或处罚策略；
- 业务数据来自 ``src/services/spam_review.py`` 的不可变快照，展示文案走 catalog
  （``antispam.review / immediate / feedback / punishment / message_type / reason``）；
- escape 策略：``recognized_text`` 截断后 escape、``reason_codes`` 解析后按 code
  渲染或 escape 兼容旧格式；``offender / operator`` mention 由调用方传入已 escape
  的 HTML（``format_user_mention`` / ``format_trusted_user_mention`` 产物），
  渲染层直接插入不二次转义。确认模式保留并回复原消息，故 ``original_text`` 不
  复制进提示，仅随 state 供 callback 处罚时取用。

参考范式：``src/bot/handlers/verification_render.py``。
"""

from typing import Literal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.core.i18n.translator import BoundLocalizer
from src.core.utils import escape_html
from src.services.spam_review import SpamMessageType, SpamReviewState

type ReviewAction = Literal["ban", "false_positive"]
type FeedbackType = Literal["normal", "spam"]
type PunishmentKey = Literal["temporary_ban", "mute", "permanent_ban"]

# 识别内容展示限长（截断后再 escape，避免超长 OCR 撑爆提示消息）
_RECOGNIZED_TEXT_LIMIT = 200


def message_type_label(localizer: BoundLocalizer, message_type: SpamMessageType) -> str:
    """将稳定的消息类型 code 映射为当前 locale 的展示文案。"""
    return localizer.t(f"antispam.message_type.{message_type.value}.label")


def punishment_label(localizer: BoundLocalizer, key: PunishmentKey) -> str:
    """将稳定的处罚 code 映射为当前 locale 的展示文案。"""
    return localizer.t(f"antispam.punishment.{key}.label")


# 需要参数的 code 及其必需参数集（缺参数则按旧格式 escape 原样显示）
# 防 AI 自由文本恰好为纯 code 名（如 "rule_match"）导致 catalog 缺占位符
_REQUIRED_REASON_PARAMS: dict[str, frozenset[str]] = {
    "rule_match": frozenset({"rule_id"}),
    "suspicious_domain": frozenset({"domain"}),
    "contact_info": frozenset({"type"}),
    "ml_classifier": frozenset({"confidence"}),
    "embedding_similarity": frozenset({"similarity"}),
    "reply_relevant": frozenset({"similarity"}),
    "topic_consistent": frozenset({"similarity"}),
}

# 数值参数 code → 参数名：confidence/similarity 为服务端 :.2f 格式化产物，
# 与 rule_match/suspicious_domain 对齐做防御性 escape（防 AI 自由文本伪造
# code 格式导致 HTML 注入）。all_detectors_failed 无参数，走通用分支。
_NUMERIC_PARAM_CODE_FIELD: dict[str, str] = {
    "ml_classifier": "confidence",
    "embedding_similarity": "similarity",
    "reply_relevant": "similarity",
    "topic_consistent": "similarity",
}

# 白名单已知 code（无必需参数的 code 也列入）
_KNOWN_REASON_CODES = frozenset(
    {
        "tg_invite",
        "rule_match",
        "suspicious_domain",
        "short_link",
        "contact_info",
        "repeated_chars",
        "channel_mention",
        "emoji_flood",
        "ml_classifier",
        "embedding_similarity",
        "all_detectors_failed",
        "reply_relevant",
        "topic_consistent",
    }
)


def _parse_reason_code(reason: str) -> tuple[str, dict[str, str]]:
    """解析编码字符串为 (code, params)。

    格式: "code" 或 "code:key=value"。每种 code 最多 1 个参数，
    value 取第一个 ``=`` 后的全部内容（保留逗号，避免 description
    含逗号时被截断）。
    """
    if ":" not in reason:
        return reason, {}
    code, params_str = reason.split(":", 1)
    if "=" not in params_str:
        return code, {}
    key, value = params_str.split("=", 1)
    return code, {key.strip(): value.strip()}


def _format_single_reason(localizer: BoundLocalizer, reason: str) -> str:
    """渲染单条原因（code 化 + 兼容旧格式）。

    - code 化原因: "tg_invite" → catalog "antispam.reason.tg_invite.label"
    - 旧格式/AI 自由文本: escape_html 原样显示
    - 缺必需参数的 code（含 AI 自由文本恰好匹配 code 名）→ escape 原样显示
    """
    code, params = _parse_reason_code(reason)

    if code not in _KNOWN_REASON_CODES:
        # 旧格式字符串或 AI 自由文本，escape 原样显示
        return escape_html(reason)

    # 校验必需参数：缺失则按旧格式 escape（防 TranslationError / 显示裸 catalog key）
    required = _REQUIRED_REASON_PARAMS.get(code)
    if required and not required <= params.keys():
        return escape_html(reason)

    # contact_info 特殊处理：type 子 code 需二次映射
    if code == "contact_info":
        contact_type = params["type"]
        # type 是子 code(wechat/qq/phone)，映射为独立 label
        type_label = localizer.t(f"antispam.reason.contact_type.{contact_type}.label")
        return localizer.t("antispam.reason.contact_info.label", type=type_label)

    # 内置规则按稳定 rule_id 从 catalog 渲染；自定义 JSON 规则 catalog 缺失时
    # 回退为经过转义的 rule_id（不展示中文 description，避免跨 locale 泄漏）。
    if code == "rule_match":
        rule_id = params["rule_id"]
        rule_key = f"antispam.reason.rule.{rule_id}.label"
        try:
            rule_label = localizer.t(rule_key)
            if rule_label == rule_key:
                rule_label = escape_html(rule_id)
        except KeyError:
            rule_label = escape_html(rule_id)
        return localizer.t("antispam.reason.rule_match.label", rule=rule_label)

    # suspicious_domain 需注入 domain 占位符（技术标识符，escape 防注入）
    if code == "suspicious_domain":
        return localizer.t(
            "antispam.reason.suspicious_domain.label",
            domain=escape_html(params["domain"]),
        )

    # 数值参数 code（ml_classifier/embedding_similarity/reply_relevant/topic_consistent）：
    # confidence/similarity 防御性 escape 后注入（与 rule_match/suspicious_domain 对齐）
    numeric_field = _NUMERIC_PARAM_CODE_FIELD.get(code)
    if numeric_field:
        return localizer.t(
            f"antispam.reason.{code}.label",
            **{numeric_field: escape_html(params[numeric_field])},
        )

    # 据 code 选 catalog key 渲染
    try:
        return localizer.t(f"antispam.reason.{code}.label", **params)
    except KeyError:
        # catalog 缺失（不应发生），降级为原样 escape
        return escape_html(reason)


def _format_reasons(localizer: BoundLocalizer, reason_codes: tuple[str, ...]) -> str:
    """渲染原因列表（code 化 + 兼容旧格式/AI 自由文本）。"""
    rendered = [_format_single_reason(localizer, reason) for reason in reason_codes]
    return localizer.t("antispam.reason.separator.label").join(rendered)


def _format_confidence(confidence: float) -> str:
    """以两位小数百分比展示置信度。"""
    return f"{confidence:.2%}"


def build_review_prompt(
    localizer: BoundLocalizer,
    state: SpamReviewState,
    offender_mention: str,
) -> str:
    """渲染确认模式提示正文（管理员 header 由调用方前置拼接）。

    ``offender_mention`` 已由调用方 escape，作为可信 HTML 直接插入。``original_text``
    与 ``recognized_text`` 均截断后 escape 作为证据展示：确认模式虽保留原消息，但
    ``message.answer`` 不建立 reply 关联，管理员需在提示内直接看到判断依据
    （codex 3b-3 review P1）。
    """
    variables = {
        "message_type": message_type_label(localizer, state.message_type),
        "user": offender_mention,
        "confidence": _format_confidence(state.confidence),
        "reasons": _format_reasons(localizer, state.reason_codes),
        "original": escape_html(state.original_text[:_RECOGNIZED_TEXT_LIMIT]),
    }
    if state.recognized_text:
        return localizer.t(
            "antispam.review.prompt_with_recognized.message",
            **variables,
            recognized=escape_html(state.recognized_text[:_RECOGNIZED_TEXT_LIMIT]),
        )
    return localizer.t("antispam.review.prompt.message", **variables)


def build_review_keyboard(
    localizer: BoundLocalizer,
    orig_msg_id: int,
    review_id: str,
) -> InlineKeyboardMarkup:
    """渲染确认模式的两个操作按钮（ban / false_positive 同行）。

    callback_data 携带 ``review_id``，consumer 据此按快照身份消费，防止旧提示按钮
    在 state 被重建后误消费新快照（codex 3b-2 review P2）。
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t("antispam.review.ban.button"),
                    callback_data=f"spam_review:ban:{orig_msg_id}:{review_id}",
                ),
                InlineKeyboardButton(
                    text=localizer.t("antispam.review.false_positive.button"),
                    callback_data=f"spam_review:false_positive:{orig_msg_id}:{review_id}",
                ),
            ]
        ]
    )


def build_review_ban_result(
    localizer: BoundLocalizer,
    operator_mention: str,
    punishment_key: PunishmentKey,
) -> str:
    """渲染确认垃圾后的结果段（追加到原提示并移除按钮）。"""
    return localizer.t(
        "antispam.review.ban.completed.message",
        operator=operator_mention,
        punishment=punishment_label(localizer, punishment_key),
    )


def build_review_false_positive_result(
    localizer: BoundLocalizer,
    operator_mention: str,
) -> str:
    """渲染确认误判后的结果段（追加到原提示并移除按钮，保留原消息）。"""
    return localizer.t(
        "antispam.review.false_positive.completed.message",
        operator=operator_mention,
    )


def build_immediate_processed(
    localizer: BoundLocalizer,
    *,
    message_type: SpamMessageType,
    offender_mention: str,
    reason_codes: tuple[str, ...],
    confidence: float,
    punishment_key: PunishmentKey,
    message_id: int,
) -> str:
    """渲染立即处罚通知正文（不含管理员 header，由调用方拼接）。"""
    return localizer.t(
        "antispam.immediate.processed.message",
        message_type=message_type_label(localizer, message_type),
        user=offender_mention,
        reasons=_format_reasons(localizer, reason_codes),
        confidence=_format_confidence(confidence),
        punishment=punishment_label(localizer, punishment_key),
        message_id=message_id,
    )


def build_immediate_keyboard(
    localizer: BoundLocalizer,
    user_id: int,
    message_id: int,
) -> InlineKeyboardMarkup:
    """渲染立即处罚后的两个反馈按钮（normal / spam 同行，事后纠正）。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t("antispam.feedback.normal.button"),
                    callback_data=f"spam_feedback:normal:{user_id}:{message_id}",
                ),
                InlineKeyboardButton(
                    text=localizer.t("antispam.feedback.spam.button"),
                    callback_data=f"spam_feedback:spam:{user_id}:{message_id}",
                ),
            ]
        ]
    )


def build_feedback_result(
    localizer: BoundLocalizer,
    is_spam: bool,
    operator_mention: str,
) -> str:
    """渲染立即模式反馈完成后的结果段（追加到处罚通知并移除按钮）。"""
    feedback_type: FeedbackType = "spam" if is_spam else "normal"
    return localizer.t(
        f"antispam.feedback.{feedback_type}.completed.message",
        operator=operator_mention,
    )
