"""验证挑战展示层

将 ``VerificationService`` 返回的结构化挑战按 locale 渲染为 Telegram 可发送的
``(text, keyboard, photo)``。所有类型文案走 catalog：

- math：独立 envelope/body（``verification.math.challenge.envelope.<flow>.message``
  + ``verification.math.challenge.body.message``），题面表达式渲染进图片
- 文字题（math/slider/qa/emoji/honeypot）：题面核心经 ``src/services/text_image``
  渲染为随机化 PNG，caption 只保留信封与说明，题面不再以文本暴露
- slider/qa/emoji/honeypot：body（``verification.<type>.challenge.body.message``）
  + 共享信封（``verification.challenge.envelope.<flow>.message``）
- 题库：QA 文案 ``verification.qa.bank.<id>.*``，Emoji 描述
  ``verification.emoji.bank.<id>.description``
- 按钮：captcha / honeypot / webapp 按钮文案各自 catalog key

所有用户可控文本（username / chat_title）在此统一 ``escape_html``，
调用方传入原始文本即可。题库文案来自受信任 catalog，原样插入不转义。
题面文本进入图片渲染，不经 HTML 转义。
"""

from collections.abc import Callable
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
from loguru import logger

from src.core.i18n.translator import BoundLocalizer
from src.core.utils import escape_html
from src.services.text_image import render_slider_image, render_text_image
from src.services.verification import (
    CaptchaChallenge,
    EmojiChallenge,
    HoneypotChallenge,
    MathChallenge,
    PuzzleChallenge,
    QAChallenge,
    ScriptChallenge,
    SliderChallenge,
    VerificationChallenge,
    WebAppChallenge,
)

type VerificationFlow = Literal["join", "join_request"]
type VerificationKeyboard = InlineKeyboardMarkup | ReplyKeyboardMarkup

# QA 原始选项 index 0-3 对应 catalog token a-d（option_order 映射用）
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
    """验证信封：标题 + 来源群 + body（body 已是可信 HTML）

    math 图片化后从「完整 message」并入标准结构，与其余类型共用信封。
    """
    return localizer.t(
        f"verification.challenge.envelope.{flow}.message",
        chat_title=chat_title,
        body=body,
    )


def _text_challenge_payload(
    *,
    localizer: BoundLocalizer,
    flow: VerificationFlow,
    safe_chat_title: str,
    body: str,
    keyboard: VerificationKeyboard,
    question: str,
    render_photo: Callable[[], BufferedInputFile],
) -> RenderedChallenge:
    """文字题统一装配：图片优先，渲染失败降级为文本题面。

    验证是入群关键路径，图片渲染不能成为单点失败——降级时题面回填 caption
    （回到图片化之前的文本形态），保证用户始终能读到题目并完成验证。
    """
    try:
        photo: BufferedInputFile | None = render_photo()
    except Exception:
        logger.exception("题面图片渲染失败，降级为文本题面")
        photo = None
    if photo is None:
        body = f"{body}\n\n❓ {escape_html(question)}"
    return RenderedChallenge(
        text=_envelope(localizer, flow, safe_chat_title, body),
        keyboard=keyboard,
        photo=photo,
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
    chat_title: str | None,
    state_token: str = "",
) -> RenderedChallenge:
    """按 locale 渲染验证挑战为可发送消息（caption + 可选题面图片）

    文字题（math/slider/qa/emoji/honeypot）题面渲染进随机化图片，caption 只保留
    信封与说明；captcha/puzzle 沿用既有 photo。username / chat_title 在此统一
    escape_html，调用方传原始文本；题面文本进图片，不经转义。
    """
    safe_username = escape_html(username)
    safe_chat_title = (
        escape_html(chat_title) if chat_title else localizer.t("common.chat.untitled_group.label")
    )

    if isinstance(challenge, MathChallenge):
        if len(challenge.choices) != 4:
            raise ValueError("数学验证必须包含 4 个选项")
        labels = tuple(str(choice) for choice in challenge.choices)
        keyboard: VerificationKeyboard = _inline_choices(
            "verify_math", chat_id, user_id, labels, labels, row_size=2
        )
        body = localizer.t(
            "verification.math.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        return _text_challenge_payload(
            localizer=localizer,
            flow=flow,
            safe_chat_title=safe_chat_title,
            body=body,
            keyboard=keyboard,
            question=f"{challenge.expression} = ?",
            render_photo=lambda: render_text_image(
                f"{challenge.expression} = ?", locale=localizer.locale
            ),
        )

    if isinstance(challenge, SliderChallenge):
        if len(challenge.cells) != 4:
            raise ValueError("滑块验证必须包含 4 个位置")
        green_positions = [index for index, cell in enumerate(challenge.cells) if cell == "🟩"]
        if len(green_positions) != 1:
            raise ValueError("滑块验证必须恰好包含一个绿色方块")
        body = localizer.t(
            "verification.slider.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        keyboard = _inline_choices(
            "verify_slider",
            chat_id,
            user_id,
            ("1", "2", "3", "4"),
            ("0", "1", "2", "3"),
            row_size=4,
        )
        return _text_challenge_payload(
            localizer=localizer,
            flow=flow,
            safe_chat_title=safe_chat_title,
            body=body,
            keyboard=keyboard,
            question="".join(challenge.cells),
            render_photo=lambda: render_slider_image(green_positions[0]),
        )

    if isinstance(challenge, ScriptChallenge):
        # 自定义脚本题：题面来自脚本动态文本，图片化 + 失败降级复用文字题装配；
        # 按钮模式 token 是索引（value 存服务端映射，不进 callback_data），
        # 文本模式（无按钮）复用 captcha 输入协议按钮
        if challenge.options:
            # callback 携带出题 token：旧验证消息按钮的 token 与新会话主键必不
            # 匹配（token 算入 session），旧按钮点击被拦为 expired
            labels = tuple(option.text for option in challenge.options)
            tokens = tuple(f"{state_token}:{index}" for index in range(len(challenge.options)))
            keyboard = _inline_choices(
                "verify_script", chat_id, user_id, labels, tokens, row_size=2
            )
        else:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=localizer.t("verification.script.challenge.input.button"),
                            callback_data=f"verify_captcha_input:{chat_id}:{user_id}",
                        )
                    ]
                ]
            )
        body = localizer.t(
            "verification.script.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        return _text_challenge_payload(
            localizer=localizer,
            flow=flow,
            safe_chat_title=safe_chat_title,
            body=body,
            keyboard=keyboard,
            question=challenge.text,
            render_photo=lambda: render_text_image(challenge.text, locale=localizer.locale),
        )

    if isinstance(challenge, QAChallenge):
        # option_order 必须是 0-3 的完整排列，否则映射无意义（防御性校验）
        if len(challenge.option_order) != len(_QA_OPTION_TOKENS) or set(
            challenge.option_order
        ) != set(range(len(_QA_OPTION_TOKENS))):
            raise ValueError("QA 验证 option_order 必须是 0-3 的完整排列")

        base = f"verification.qa.bank.{challenge.question_id}"
        question = localizer.t(f"{base}.question")
        # 按打乱顺序取选项：第 i 个按钮显示原始 option_order[i] 对应的文案
        options = tuple(
            localizer.t(f"{base}.option_{_QA_OPTION_TOKENS[origin]}")
            for origin in challenge.option_order
        )
        body = localizer.t(
            "verification.qa.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        keyboard = _inline_choices(
            "verify_qa", chat_id, user_id, options, ("0", "1", "2", "3"), row_size=2
        )
        return _text_challenge_payload(
            localizer=localizer,
            flow=flow,
            safe_chat_title=safe_chat_title,
            body=body,
            keyboard=keyboard,
            question=question,
            render_photo=lambda: render_text_image(question, locale=localizer.locale),
        )

    if isinstance(challenge, EmojiChallenge):
        if len(challenge.emojis) != 4:
            raise ValueError("Emoji 验证必须包含 4 个选项")
        description = localizer.t(f"verification.emoji.bank.{challenge.description_id}.description")
        body = localizer.t(
            "verification.emoji.challenge.body.message",
            username=safe_username,
            timeout=timeout,
        )
        keyboard = _inline_choices(
            "verify_emoji", chat_id, user_id, challenge.emojis, ("0", "1", "2", "3"), row_size=2
        )
        return _text_challenge_payload(
            localizer=localizer,
            flow=flow,
            safe_chat_title=safe_chat_title,
            body=body,
            keyboard=keyboard,
            question=description,
            render_photo=lambda: render_text_image(description, locale=localizer.locale),
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
        )
        return _text_challenge_payload(
            localizer=localizer,
            flow=flow,
            safe_chat_title=safe_chat_title,
            body=body,
            keyboard=keyboard,
            question=f"{challenge.expression} = ?",
            render_photo=lambda: render_text_image(
                f"{challenge.expression} = ?", locale=localizer.locale
            ),
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
