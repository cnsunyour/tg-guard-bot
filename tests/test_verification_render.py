"""验证挑战渲染层测试。

覆盖 3a-1 重构核心契约：
- 8 种 challenge 类型渲染（math 走 catalog，其余中文硬编码）
- callback_data 格式与原实现一致
- escape_html 正确（无双重转义、无遗漏）
- captcha refresh 独立 body（不含信封）
- flow（join/join_request）影响信封标题
"""

import pytest
from aiogram.types import BufferedInputFile, ReplyKeyboardMarkup

from src.bot.handlers.verification_render import (
    render_captcha_for_refresh,
    render_verification_challenge,
)
from src.core.i18n.translator import Translator
from src.services.verification import (
    CaptchaChallenge,
    EmojiChallenge,
    HoneypotChallenge,
    MathChallenge,
    PuzzleChallenge,
    QAChallenge,
    SliderChallenge,
    WebAppChallenge,
)

pytestmark = pytest.mark.unit

_CHAT_ID = -100
_USER_ID = 42
_TIMEOUT = 120


def _make_translator() -> Translator:
    """含 math 挑战文案的小 catalog（zh-Hans）"""
    return Translator(
        {
            "zh-Hans": {
                "verification.math.challenge.join.message": (
                    "join | {username} | {chat_title} | {timeout} | {expression}"
                ),
                "verification.math.challenge.join_request.message": (
                    "join_request | {username} | {chat_title} | {timeout} | {expression}"
                ),
            }
        },
        default_locale="zh-Hans",
    )


def _render(challenge, *, flow="join", username="Alice", chat_title="TestGroup"):
    return render_verification_challenge(
        challenge,
        _make_translator().for_locale("zh-Hans"),
        chat_id=_CHAT_ID,
        user_id=_USER_ID,
        flow=flow,
        timeout=_TIMEOUT,
        username=username,
        chat_title=chat_title,
    )


def _buttons(rendered) -> list:
    """展开 inline keyboard 所有按钮"""
    return [b for row in rendered.keyboard.inline_keyboard for b in row]


def _photo() -> BufferedInputFile:
    return BufferedInputFile(b"data", filename="x.png")


# ===== math =====


def test_math_render_uses_catalog_and_four_buttons() -> None:
    rendered = _render(MathChallenge(expression="3 + 5", choices=(8, 3, 12, 7)))
    assert rendered.text == "join | Alice | TestGroup | 120 | 3 + 5"
    assert rendered.photo is None
    buttons = _buttons(rendered)
    assert [b.text for b in buttons] == ["8", "3", "12", "7"]
    assert {b.callback_data for b in buttons} == {
        f"verify_math:{_CHAT_ID}:{_USER_ID}:{v}" for v in (8, 3, 12, 7)
    }


def test_math_render_escapes_username_chat_title_expression() -> None:
    rendered = _render(
        MathChallenge(expression="1 < 2 & 3", choices=(3, 1, 2, 4)),
        username="<b>x</b>",
        chat_title="A&B",
    )
    assert "<b>x</b>" not in rendered.text
    assert "&lt;b&gt;x&lt;/b&gt;" in rendered.text
    assert "A&amp;B" in rendered.text
    assert "1 &lt; 2 &amp; 3" in rendered.text


# ===== slider =====


def test_slider_render_join_envelope_and_buttons() -> None:
    rendered = _render(SliderChallenge(cells=("🟩", "⬜", "⬜", "⬜")))
    assert "🟩⬜⬜⬜" in rendered.text
    assert "群组验证通知" in rendered.text
    assert "您加入了群组" in rendered.text
    assert [b.callback_data for b in _buttons(rendered)] == [
        f"verify_slider:{_CHAT_ID}:{_USER_ID}:{i}" for i in range(4)
    ]


def test_slider_render_join_request_envelope() -> None:
    rendered = _render(SliderChallenge(cells=("⬜", "🟩", "⬜", "⬜")), flow="join_request")
    assert "加入请求验证" in rendered.text
    assert "您请求加入群组" in rendered.text


# ===== qa / emoji =====


def test_qa_render_shows_question_and_buttons() -> None:
    rendered = _render(QAChallenge(question="一年几个月？", options=("10", "11", "12", "13")))
    assert "一年几个月？" in rendered.text
    assert [b.text for b in _buttons(rendered)] == ["10", "11", "12", "13"]


def test_emoji_render_shows_description_and_buttons() -> None:
    rendered = _render(EmojiChallenge(description="开心", emojis=("😊", "😢", "😡", "😴")))
    assert "开心" in rendered.text
    assert [b.text for b in _buttons(rendered)] == ["😊", "😢", "😡", "😴"]


# ===== captcha =====


def test_captcha_render_has_photo_and_two_buttons() -> None:
    photo = _photo()
    rendered = _render(CaptchaChallenge(photo=photo))
    assert rendered.photo is photo
    assert "群组验证通知" in rendered.text  # 初次发送含信封
    assert [b.callback_data for b in _buttons(rendered)] == [
        f"verify_captcha_input:{_CHAT_ID}:{_USER_ID}",
        f"verify_captcha_refresh:{_CHAT_ID}:{_USER_ID}",
    ]


def test_captcha_refresh_render_body_only_without_envelope() -> None:
    rendered = render_captcha_for_refresh(
        CaptchaChallenge(photo=_photo()),
        chat_id=_CHAT_ID,
        user_id=_USER_ID,
        username="Alice",
        timeout=_TIMEOUT,
    )
    assert "群组验证通知" not in rendered.text
    assert "加入请求验证" not in rendered.text
    assert "Alice" in rendered.text


# ===== honeypot =====


@pytest.mark.parametrize(
    ("decoy", "expected_text"),
    [
        ("skip", "✅ 跳过验证"),
        ("direct", "✅ 直接通过"),
        ("human", "✅ 我是人类"),
    ],
)
def test_honeypot_render_trap_and_answer_buttons(decoy: str, expected_text: str) -> None:
    rendered = _render(HoneypotChallenge(expression="3 + 5", choices=(2, 8, 4), decoy=decoy))
    rows = rendered.keyboard.inline_keyboard
    assert len(rows) == 2
    assert rows[0][0].text == expected_text
    assert rows[0][0].callback_data == f"verify_honeypot:{_CHAT_ID}:{_USER_ID}:trap"
    assert [b.callback_data for b in rows[1]] == [
        f"verify_honeypot:{_CHAT_ID}:{_USER_ID}:{v}" for v in (2, 8, 4)
    ]
    assert "3 + 5" in rendered.text


# ===== puzzle =====


def test_puzzle_render_has_photo_and_four_buttons() -> None:
    photo = _photo()
    rendered = _render(PuzzleChallenge(photo=photo))
    assert rendered.photo is photo
    assert [b.callback_data for b in _buttons(rendered)] == [
        f"verify_puzzle:{_CHAT_ID}:{_USER_ID}:{i}" for i in range(4)
    ]


# ===== WebApp =====


def test_webapp_render_reply_keyboard_with_url() -> None:
    url = "https://example.com/turnstile.html?chat_id=-100&user_id=42&token=abc"
    rendered = _render(WebAppChallenge(provider="turnstile", webapp_url=url))
    assert isinstance(rendered.keyboard, ReplyKeyboardMarkup)
    button = rendered.keyboard.keyboard[0][0]
    assert button.web_app is not None
    assert button.web_app.url == url
    assert button.text == "🔐 开始验证"
    assert rendered.photo is None


# ===== 选项数量校验 =====


def test_math_invalid_choice_count_raises() -> None:
    with pytest.raises(ValueError):
        _render(MathChallenge(expression="1+1", choices=(2, 3)))


def test_slider_invalid_cell_count_raises() -> None:
    with pytest.raises(ValueError):
        _render(SliderChallenge(cells=("🟩", "⬜")))
