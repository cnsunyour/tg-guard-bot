"""验证挑战展示层

将 ``VerificationService`` 返回的结构化挑战按 locale 渲染为 Telegram 可发送的
``(text, keyboard, photo)``。service 只产业务数据，本模块负责展示：

- math：文案走 catalog（已 i18n），按 flow 取 ``verification.math.challenge.<flow>.message``
- 其余类型：3a-1 暂保留中文硬编码（从原 service 搬运，行为不变），3a-2 再迁 catalog

所有用户可控文本（username / chat_title / expression）在此统一 ``escape_html``，
避免双重转义——调用方传入原始文本即可。
"""

from dataclasses import dataclass
from typing import Literal, assert_never

from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from src.core.i18n.translator import BoundLocalizer
from src.core.utils import escape_html
from src.services.verification import (
    CaptchaChallenge,
    EmojiChallenge,
    HoneypotChallenge,
    HoneypotDecoy,
    MathChallenge,
    PuzzleChallenge,
    QAChallenge,
    SliderChallenge,
    VerificationChallenge,
    WebAppChallenge,
)

type VerificationFlow = Literal["join", "join_request"]
type VerificationKeyboard = InlineKeyboardMarkup | ReplyKeyboardMarkup

# 蜜罐诱饵 code → 展示文本（3a-1 中文硬编码，3a-2 迁 catalog）
_HONEYPOT_DECOY_TEXT: dict[HoneypotDecoy, str] = {
    "skip": "✅ 跳过验证",
    "direct": "✅ 直接通过",
    "human": "✅ 我是人类",
}


@dataclass(frozen=True, slots=True)
class RenderedChallenge:
    """渲染产物：可直接用于 bot.send_message / send_photo"""

    text: str
    keyboard: VerificationKeyboard
    photo: BufferedInputFile | None = None


def _inline_choices(
    prefix: str,
    chat_id: int,
    user_id: int,
    labels: tuple[str, ...],
    tokens: tuple[str, ...],
    row_size: int,
) -> InlineKeyboardMarkup:
    """构造选项按钮 keyboard

    callback_data 格式：``{prefix}:{chat_id}:{user_id}:{token}``；
    labels 与 tokens 等长，按 row_size 自动分行。
    """
    if len(labels) != len(tokens):
        raise ValueError("验证按钮 label/token 数量不一致")
    buttons = [
        InlineKeyboardButton(
            text=label,
            callback_data=f"{prefix}:{chat_id}:{user_id}:{token}",
        )
        for label, token in zip(labels, tokens, strict=True)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            buttons[index : index + row_size] for index in range(0, len(buttons), row_size)
        ]
    )


def _envelope(flow: VerificationFlow, chat_title: str, body: str) -> str:
    """非 math 验证的信封：标题 + 来源群 + body（3a-1 保持原中文行为）"""
    if flow == "join_request":
        title = "加入请求验证"
        prefix = "您请求加入群组"
    elif flow == "join":
        title = "群组验证通知"
        prefix = "您加入了群组"
    else:
        raise ValueError(f"不支持的验证流程: {flow}")
    return f"📢 <b>{title}</b>\n\n{prefix}：<b>{chat_title}</b>\n\n{body}"


def _captcha_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    """captcha 两个操作按钮"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ 输入验证码",
                    callback_data=f"verify_captcha_input:{chat_id}:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 换一张",
                    callback_data=f"verify_captcha_refresh:{chat_id}:{user_id}",
                )
            ],
        ]
    )


def _captcha_body(username: str, timeout: int) -> str:
    """captcha body 文案（不含信封，刷新时单独使用）"""
    return (
        f"👋 欢迎 {escape_html(username)}！\n\n"
        f"请在 {timeout} 秒内输入图片中的验证码（不区分大小写）："
    )


def render_captcha_for_refresh(
    challenge: CaptchaChallenge,
    chat_id: int,
    user_id: int,
    username: str,
    timeout: int,
) -> RenderedChallenge:
    """captcha 刷新专用：caption 只显示 body（保持原 ``on_captcha_refresh`` 行为）

    与初次发送不同，刷新后不重复信封标题，仅更新题面与按钮。
    """
    return RenderedChallenge(
        text=_captcha_body(username, timeout),
        keyboard=_captcha_keyboard(chat_id, user_id),
        photo=challenge.photo,
    )


def render_verification_challenge(
    challenge: VerificationChallenge,
    localizer: BoundLocalizer,
    chat_id: int,
    user_id: int,
    flow: VerificationFlow,
    timeout: int,
    *,
    username: str,
    chat_title: str,
) -> RenderedChallenge:
    """按 locale 渲染验证挑战为可发送消息

    math 走 catalog（已 i18n）；其余类型 3a-1 暂保留中文硬编码，3a-2 迁 catalog。
    username / chat_title / expression 在此统一 escape_html，调用方传原始文本。
    """
    safe_username = escape_html(username)
    safe_chat_title = escape_html(chat_title)

    if isinstance(challenge, MathChallenge):
        if len(challenge.choices) != 4:
            raise ValueError("数学验证必须包含 4 个选项")
        labels = tuple(str(choice) for choice in challenge.choices)
        keyboard: VerificationKeyboard = _inline_choices(
            "verify_math", chat_id, user_id, labels, labels, row_size=2
        )
        text = localizer.t(
            f"verification.math.challenge.{flow}.message",
            username=safe_username,
            chat_title=safe_chat_title,
            expression=escape_html(challenge.expression),
            timeout=timeout,
        )
        return RenderedChallenge(text=text, keyboard=keyboard)

    if isinstance(challenge, SliderChallenge):
        if len(challenge.cells) != 4:
            raise ValueError("滑块验证必须包含 4 个位置")
        body = (
            f"👋 欢迎 {safe_username}！\n\n"
            f"请在 {timeout} 秒内点击绿色方块：\n\n{''.join(challenge.cells)}"
        )
        keyboard = _inline_choices(
            "verify_slider", chat_id, user_id, challenge.cells, ("0", "1", "2", "3"), row_size=4
        )
        return RenderedChallenge(text=_envelope(flow, safe_chat_title, body), keyboard=keyboard)

    if isinstance(challenge, QAChallenge):
        if len(challenge.options) != 4:
            raise ValueError("问答验证必须包含 4 个选项")
        body = (
            f"👋 欢迎 {safe_username}！\n\n"
            f"请在 {timeout} 秒内回答问题：\n\n❓ {challenge.question}"
        )
        keyboard = _inline_choices(
            "verify_qa", chat_id, user_id, challenge.options, ("0", "1", "2", "3"), row_size=2
        )
        return RenderedChallenge(text=_envelope(flow, safe_chat_title, body), keyboard=keyboard)

    if isinstance(challenge, EmojiChallenge):
        if len(challenge.emojis) != 4:
            raise ValueError("Emoji 验证必须包含 4 个选项")
        body = (
            f"👋 欢迎 {safe_username}！\n\n"
            f"请在 {timeout} 秒内选择对应的表情：\n\n❓ {challenge.description}"
        )
        keyboard = _inline_choices(
            "verify_emoji", chat_id, user_id, challenge.emojis, ("0", "1", "2", "3"), row_size=2
        )
        return RenderedChallenge(text=_envelope(flow, safe_chat_title, body), keyboard=keyboard)

    if isinstance(challenge, CaptchaChallenge):
        return RenderedChallenge(
            text=_envelope(flow, safe_chat_title, _captcha_body(username, timeout)),
            keyboard=_captcha_keyboard(chat_id, user_id),
            photo=challenge.photo,
        )

    if isinstance(challenge, HoneypotChallenge):
        if len(challenge.choices) != 3:
            raise ValueError("蜜罐验证必须包含 3 个真实选项")
        decoy_text = _HONEYPOT_DECOY_TEXT[challenge.decoy]
        answer_buttons = [
            InlineKeyboardButton(
                text=str(choice),
                callback_data=f"verify_honeypot:{chat_id}:{user_id}:{choice}",
            )
            for choice in challenge.choices
        ]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=decoy_text,
                        callback_data=f"verify_honeypot:{chat_id}:{user_id}:trap",
                    )
                ],
                answer_buttons,
            ]
        )
        body = (
            f"👋 欢迎 {safe_username}！\n\n"
            f"请在 {timeout} 秒内回答问题：\n\n"
            f"❓ {escape_html(challenge.expression)} = ?"
        )
        return RenderedChallenge(text=_envelope(flow, safe_chat_title, body), keyboard=keyboard)

    if isinstance(challenge, PuzzleChallenge):
        keyboard = _inline_choices(
            "verify_puzzle",
            chat_id,
            user_id,
            ("1️⃣", "2️⃣", "3️⃣", "4️⃣"),
            ("0", "1", "2", "3"),
            row_size=4,
        )
        body = f"👋 欢迎 {safe_username}！\n\n请在 {timeout} 秒内选择灰色缺口的位置："
        return RenderedChallenge(
            text=_envelope(flow, safe_chat_title, body), keyboard=keyboard, photo=challenge.photo
        )

    if isinstance(challenge, WebAppChallenge):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="🔐 开始验证",
                        web_app=WebAppInfo(url=challenge.webapp_url),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        body = f"👋 欢迎 {safe_username}！\n\n请在 {timeout} 秒内点击下方按钮完成验证："
        return RenderedChallenge(text=_envelope(flow, safe_chat_title, body), keyboard=keyboard)

    assert_never(challenge)
