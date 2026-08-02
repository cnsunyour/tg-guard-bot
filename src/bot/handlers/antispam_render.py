"""反垃圾提示纯渲染层。

将 ``SpamReviewState``（确认模式）或即时检测字段（立即处罚模式）按 locale 渲染为
Telegram HTML 文本与内联键盘。本模块是纯展示层：

- 不注册 aiogram handler、不访问 Redis、不决定检测或处罚策略；
- 业务数据来自 ``src/services/spam_review.py`` 的不可变快照，展示文案走 catalog
  （``antispam.review / immediate / feedback / punishment / message_type``）；
- escape 策略：``recognized_text`` 截断后 escape、``reason_codes`` join 后 escape；
  ``offender / operator`` mention 由调用方传入已 escape 的 HTML
  （``format_user_mention`` / ``format_trusted_user_mention`` 产物），渲染层直接
  插入不二次转义。确认模式保留并回复原消息，故 ``original_text`` 不复制进提示，
  仅随 state 供 callback 处罚时取用。

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


def _format_reasons(reason_codes: tuple[str, ...]) -> str:
    """连接并转义状态中的原始原因串（暂未稳定化，按原样 escape 展示）。"""
    return escape_html("、".join(reason_codes))


def _format_confidence(confidence: float) -> str:
    """以两位小数百分比展示置信度。"""
    return f"{confidence:.2%}"


def build_review_prompt(
    localizer: BoundLocalizer,
    state: SpamReviewState,
    offender_mention: str,
) -> str:
    """渲染确认模式提示正文（不含管理员 header，由调用方拼 ``🔔{admins}\\n\\n``）。

    ``offender_mention`` 已由调用方 escape，作为可信 HTML 直接插入。确认模式保留
    原消息，故 ``original_text`` 不复制进提示；``recognized_text`` 非空时走带识别
    内容的 key 并截断后 escape。
    """
    variables = {
        "message_type": message_type_label(localizer, state.message_type),
        "user": offender_mention,
        "confidence": _format_confidence(state.confidence),
        "reasons": _format_reasons(state.reason_codes),
    }
    if state.recognized_text:
        return localizer.t(
            "antispam.review.prompt_with_recognized.message",
            **variables,
            recognized=escape_html(state.recognized_text[:_RECOGNIZED_TEXT_LIMIT]),
        )
    return localizer.t("antispam.review.prompt.message", **variables)


def build_review_keyboard(localizer: BoundLocalizer, orig_msg_id: int) -> InlineKeyboardMarkup:
    """渲染确认模式的两个操作按钮（ban / false_positive 同行）。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t("antispam.review.ban.button"),
                    callback_data=f"spam_review:ban:{orig_msg_id}",
                ),
                InlineKeyboardButton(
                    text=localizer.t("antispam.review.false_positive.button"),
                    callback_data=f"spam_review:false_positive:{orig_msg_id}",
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
        reasons=_format_reasons(reason_codes),
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
