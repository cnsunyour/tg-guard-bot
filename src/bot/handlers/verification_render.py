"""验证挑战展示层

将 ``VerificationService`` 返回的结构化挑战按 locale 渲染为 Telegram 可发送的
``(text, keyboard, photo)``。所有类型文案走 catalog：

- math：完整 message（``verification.math.challenge.<flow>.message``，含 expression）
- 其余：body（``verification.<type>.challenge.body.message``）+ 共享信封
  （``verification.challenge.envelope.<flow>.message``）
- 题库：QA 文案 ``verification.qa.bank.<id>.*``，Emoji 描述
  ``verification.emoji.bank.<id>.description``
- 按钮：captcha / honeypot / webapp 按钮文案各自 catalog key

所有用户可控文本（username / chat_title / expression）在此统一 ``escape_html``，
调用方传入原始文本即可。题库文案来自受信任 catalog，原样插入不转义。
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
    MathChallenge,
    PuzzleChallenge,
    QAChallenge,
    SliderChallenge,
    VerificationChallenge,
    WebAppChallenge,
)

type VerificationFlow = Literal["join", "join_request"]
type VerificationKeyboard = InlineKeyboardMarkup | ReplyKeyboardMarkup

# QA 选项 token（a/b/c/d 对应按钮位置 0-3，与题库 correct_index 对齐）
_QA_OPTION_TOKENS: tuple[str, ...] = ("a", "b", "c", "d")


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


def _envelope(localizer: BoundLocalizer, flow: VerificationFlow, chat_title: str, body: str) -> str:
    """非 math 验证的共享信封：标题 + 来源群 + body（body 已是可信 HTML）"""
    return localizer.t(
        f"verification.challenge.envelope.{flow}.message",
        chat_title=chat_title,
        body=body,
    )


def _captcha_keyboard(
    localizer: BoundLocalizer, chat_id: int, user_id: int
) -> InlineKeyboardMarkup:
    """captcha 两个操作按钮（文案走 catalog）"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t("verification.captcha.challenge.input.button"),
                    callback_data=f"verify_captcha_input:{chat_id}:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.t("verification.captcha.challenge.refresh.button"),
                    callback_data=f"verify_captcha_refresh:{chat_id}:{user_id}",
                )
            ],
        ]
    )


def render_captcha_for_refresh(
    challenge: CaptchaChallenge,
    localizer: BoundLocalizer,
    chat_id: int,
    user_id: int,
    username: str,
    timeout: int,
) -> RenderedChallenge:
    """captcha 刷新专用：caption 只显示 body（保持原 ``on_captcha_refresh`` 行为）

    与初次发送不同，刷新后不重复信封标题，仅更新题面与按钮。
    """
    body = localizer.t(
        "verification.captcha.challenge.body.message",
        username=escape_html(username),
        timeout=timeout,
    )
    return RenderedChallenge(
        text=body,
        keyboard=_captcha_keyboard(localizer, chat_id, user_id),
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

    math 用完整 catalog message；其余类型 body + 共享信封。username / chat_title
    / expression 在此统一 escape_html，调用方传原始文本。
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
        body = localizer.t(
            "verification.slider.challenge.body.message",
            username=safe_username,
            timeout=timeout,
            cells="".join(challenge.cells),
        )
        keyboard = _inline_choices(
            "verify_slider", chat_id, user_id, challenge.cells, ("0", "1", "2", "3"), row_size=4
        )
        return RenderedChallenge(
            text=_envelope(localizer, flow, safe_chat_title, body), keyboard=keyboard
        )

    if isinstance(challenge, QAChallenge):
        base = f"verification.qa.bank.{challenge.question_id}"
        question = localizer.t(f"{base}.question")
        options = tuple(localizer.t(f"{base}.option_{token}") for token in _QA_OPTION_TOKENS)
        body = localizer.t(
            "verification.qa.challenge.body.message",
            username=safe_username,
            timeout=timeout,
            question=question,
        )
        keyboard = _inline_choices(
            "verify_qa", chat_id, user_id, options, ("0", "1", "2", "3"), row_size=2
        )
        return RenderedChallenge(
            text=_envelope(localizer, flow, safe_chat_title, body), keyboard=keyboard
        )

    if isinstance(challenge, EmojiChallenge):
        if len(challenge.emojis) != 4:
            raise ValueError("Emoji 验证必须包含 4 个选项")
        description = localizer.t(f"verification.emoji.bank.{challenge.description_id}.description")
        body = localizer.t(
            "verification.emoji.challenge.body.message",
            username=safe_username,
            timeout=timeout,
            description=description,
        )
        keyboard = _inline_choices(
            "verify_emoji", chat_id, user_id, challenge.emojis, ("0", "1", "2", "3"), row_size=2
        )
        return RenderedChallenge(
            text=_envelope(localizer, flow, safe_chat_title, body), keyboard=keyboard
        )

    if isinstance(challenge, CaptchaChallenge):
        body = localizer.t(
            "verification.captcha.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        return RenderedChallenge(
            text=_envelope(localizer, flow, safe_chat_title, body),
            keyboard=_captcha_keyboard(localizer, chat_id, user_id),
            photo=challenge.photo,
        )

    if isinstance(challenge, HoneypotChallenge):
        if len(challenge.choices) != 3:
            raise ValueError("蜜罐验证必须包含 3 个真实选项")
        decoy_text = localizer.t(f"verification.honeypot.challenge.decoy.{challenge.decoy}.button")
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
        body = localizer.t(
            "verification.honeypot.challenge.body.message",
            username=safe_username,
            timeout=timeout,
            expression=escape_html(challenge.expression),
        )
        return RenderedChallenge(
            text=_envelope(localizer, flow, safe_chat_title, body), keyboard=keyboard
        )

    if isinstance(challenge, PuzzleChallenge):
        keyboard = _inline_choices(
            "verify_puzzle",
            chat_id,
            user_id,
            ("1️⃣", "2️⃣", "3️⃣", "4️⃣"),
            ("0", "1", "2", "3"),
            row_size=4,
        )
        body = localizer.t(
            "verification.puzzle.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        return RenderedChallenge(
            text=_envelope(localizer, flow, safe_chat_title, body),
            keyboard=keyboard,
            photo=challenge.photo,
        )

    if isinstance(challenge, WebAppChallenge):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text=localizer.t("verification.webapp.challenge.start.button"),
                        web_app=WebAppInfo(url=challenge.webapp_url),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        body = localizer.t(
            "verification.webapp.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        return RenderedChallenge(
            text=_envelope(localizer, flow, safe_chat_title, body), keyboard=keyboard
        )

    assert_never(challenge)
