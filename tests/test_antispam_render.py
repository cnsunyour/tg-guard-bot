"""反垃圾渲染层测试。

覆盖 3b-2 核心契约：

- review prompt 在有 / 无 recognized_text 时选对 catalog key
- HTML escape：reason_codes / recognized_text 转义；offender / operator 等
  预转义 mention 直接插入不二次转义；确认模式保留原消息，故 original_text
  不注入提示
- recognized_text 截断 200 后再 escape
- SpamMessageType 5 值 + PunishmentKey 3 值在 catalog 精确覆盖（无孤立 key）
- callback_data 格式与按钮文案
- 三语言渲染均无 ``{xxx}`` 占位残留
"""

import json
import re
from pathlib import Path
from typing import cast

import pytest

from src.bot.handlers.antispam_render import (
    _format_confidence,
    _format_reasons,
    build_feedback_result,
    build_immediate_keyboard,
    build_immediate_processed,
    build_review_ban_result,
    build_review_false_positive_result,
    build_review_ignore_result,
    build_review_keyboard,
    build_review_prompt,
    message_type_label,
    punishment_label,
)
from src.core.i18n.translator import BoundLocalizer, Translator
from src.core.utils import escape_html
from src.services.spam_review import SpamMessageType, SpamReviewState

pytestmark = pytest.mark.unit

_LOCALES = ("zh-Hans", "zh-Hant", "en")
_PUNISHMENT_KEYS = ("temporary_ban", "mute", "permanent_ban")
_PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _load_catalog(locale: str) -> dict[str, str]:
    raw = json.loads(Path(f"locales/{locale}.json").read_text(encoding="utf-8"))
    return cast("dict[str, str]", raw)


def _make_translator() -> Translator:
    """加载三语真实 catalog，开启 strict 强制 key / 占位契约校验。"""
    catalogs = {locale: _load_catalog(locale) for locale in _LOCALES}
    return Translator(catalogs, default_locale="zh-Hans", strict=True)


def _localizer(locale: str = "zh-Hans") -> BoundLocalizer:
    return _make_translator().for_locale(locale)


def _state(
    *,
    message_type: SpamMessageType = SpamMessageType.photo,
    original_text: str = "原始 caption",
    recognized_text: str | None = "OCR 识别文本",
    reason_codes: tuple[str, ...] = ("rule:url", "vision:二维码"),
    confidence: float = 0.9125,
) -> SpamReviewState:
    return SpamReviewState(
        review_id="0123456789abcdef",
        offender_user_id=42,
        message_type=message_type,
        original_text=original_text,
        recognized_text=recognized_text,
        sample_text=original_text,
        reason_codes=reason_codes,
        confidence=confidence,
    )


def _assert_no_placeholders(text: str) -> None:
    assert _PLACEHOLDER_PATTERN.search(text) is None


def test_review_prompt_without_recognized_uses_plain_key() -> None:
    localizer = _localizer()
    unsafe_original = '<b>原文</b><script>alert("x")</script> 😀'
    state = _state(
        message_type=SpamMessageType.text,
        original_text=unsafe_original,
        recognized_text=None,
        reason_codes=('rule:<script>"x"</script>', "emoji:😀"),
    )
    offender = '<a href="tg://user?id=42">A &amp; B</a>'

    rendered = build_review_prompt(localizer, state, offender)

    assert rendered == localizer.t(
        "antispam.review.prompt.message",
        message_type=message_type_label(localizer, SpamMessageType.text),
        user=offender,
        confidence="91.25%",
        reasons=escape_html('rule:<script>"x"</script>、emoji:😀'),
        original=escape_html(unsafe_original),
    )
    # 无 recognized 时走 plain key，不含识别内容段
    assert "识别内容" not in rendered
    # offender 已预转义，直接插入不二次转义
    assert offender in rendered
    assert "&lt;a href=" not in rendered
    # reason_codes 被转义
    assert "&lt;script&gt;&quot;x&quot;&lt;/script&gt;" in rendered
    # P1：original_text 截断后 escape 展示（管理员需在提示内看到判断依据）
    assert escape_html(unsafe_original) in rendered
    assert "<script>alert" not in rendered
    _assert_no_placeholders(rendered)


def test_review_prompt_with_recognized_uses_recognized_key_and_escapes_html() -> None:
    localizer = _localizer()
    recognized = '<b>OCR</b><script>alert("x")</script> 😀 & "quoted"'
    state = _state(recognized_text=recognized)

    rendered = build_review_prompt(localizer, state, "Alice")

    assert rendered == localizer.t(
        "antispam.review.prompt_with_recognized.message",
        message_type=message_type_label(localizer, state.message_type),
        user="Alice",
        confidence=_format_confidence(state.confidence),
        reasons=_format_reasons(localizer, state.reason_codes),
        original=escape_html(state.original_text),
        recognized=escape_html(recognized),
    )
    assert escape_html(recognized) in rendered
    assert "<script>" not in rendered
    assert "&quot;quoted&quot;" in rendered
    _assert_no_placeholders(rendered)


def test_review_recognized_text_is_truncated_before_escape() -> None:
    recognized = "字" * 200 + "<OVERFLOW-MARKER>"
    state = _state(recognized_text=recognized)

    rendered = build_review_prompt(_localizer(), state, "Alice")

    assert escape_html(recognized[:200]) in rendered
    assert "OVERFLOW-MARKER" not in rendered


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.0, "0.00%"), (0.9125, "91.25%"), (1.0, "100.00%")],
)
def test_format_confidence(confidence: float, expected: str) -> None:
    assert _format_confidence(confidence) == expected


def test_format_reasons_legacy_format_escapes() -> None:
    """旧格式字符串/AI 自由文本 escape 原样显示（兼容性）。"""
    localizer = _localizer()
    # 旧格式中文字符串（非 code）
    assert _format_reasons(localizer, ("<b>rule</b>", 'vision:"qr"&😀')) == (
        "&lt;b&gt;rule&lt;/b&gt;、vision:&quot;qr&quot;&amp;😀"
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_message_type_and_punishment_catalog_keys_have_exact_coverage(locale: str) -> None:
    catalog = _load_catalog(locale)
    localizer = _localizer(locale)
    expected_message_keys = {
        f"antispam.message_type.{message_type.value}.label" for message_type in SpamMessageType
    }
    actual_message_keys = {k for k in catalog if k.startswith("antispam.message_type.")}
    expected_punishment_keys = {f"antispam.punishment.{key}.label" for key in _PUNISHMENT_KEYS}
    actual_punishment_keys = {k for k in catalog if k.startswith("antispam.punishment.")}

    # enum 成员与 catalog key 精确对应，无孤立、无缺失
    assert actual_message_keys == expected_message_keys
    assert actual_punishment_keys == expected_punishment_keys
    for message_type in SpamMessageType:
        key = f"antispam.message_type.{message_type.value}.label"
        assert message_type_label(localizer, message_type) == catalog[key]
    for punishment_key in _PUNISHMENT_KEYS:
        key = f"antispam.punishment.{punishment_key}.label"
        assert punishment_label(localizer, punishment_key) == catalog[key]


def test_review_keyboard_callback_data_and_labels() -> None:
    localizer = _localizer()
    review_id = "0123456789abcdef"
    keyboard = build_review_keyboard(localizer, orig_msg_id=321, review_id=review_id)

    # ban / false_positive 同行，ignore 单独一行（移动端三按钮同行过窄）
    assert len(keyboard.inline_keyboard) == 2
    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        "spam_review:ban:321:0123456789abcdef",
        "spam_review:false_positive:321:0123456789abcdef",
    ]
    assert [button.text for button in keyboard.inline_keyboard[0]] == [
        localizer.t("antispam.review.ban.button"),
        localizer.t("antispam.review.false_positive.button"),
    ]
    assert [button.callback_data for button in keyboard.inline_keyboard[1]] == [
        "spam_review:ignore:321:0123456789abcdef",
    ]
    assert [button.text for button in keyboard.inline_keyboard[1]] == [
        localizer.t("antispam.review.ignore.button"),
    ]


def test_immediate_keyboard_callback_data_and_labels() -> None:
    localizer = _localizer()
    keyboard = build_immediate_keyboard(localizer, user_id=42, message_id=321)

    assert len(keyboard.inline_keyboard) == 1
    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        "spam_feedback:normal:42:321",
        "spam_feedback:spam:42:321",
    ]
    assert [button.text for button in keyboard.inline_keyboard[0]] == [
        localizer.t("antispam.feedback.normal.button"),
        localizer.t("antispam.feedback.spam.button"),
    ]


def test_immediate_processed_escapes_reasons_but_not_preescaped_mention() -> None:
    localizer = _localizer()
    offender = '<a href="tg://user?id=42">A &amp; B</a>'

    rendered = build_immediate_processed(
        localizer,
        message_type=SpamMessageType.edited_photo,
        offender_mention=offender,
        reason_codes=("<script>rule</script>", 'vision:"qr"&😀'),
        confidence=0.875,
        punishment_key="temporary_ban",
        message_id=321,
    )

    assert offender in rendered
    assert "&lt;a href=" not in rendered
    assert "&lt;script&gt;rule&lt;/script&gt;" in rendered
    assert "87.50%" in rendered
    assert punishment_label(localizer, "temporary_ban") in rendered
    assert "321" in rendered
    _assert_no_placeholders(rendered)


def test_review_and_feedback_results_keep_preescaped_operator_mention() -> None:
    localizer = _localizer()
    operator = '<a href="tg://user?id=7">Root &amp; Ops</a>'
    rendered = (
        build_review_ban_result(localizer, operator, "permanent_ban"),
        build_review_false_positive_result(localizer, operator),
        build_review_ignore_result(localizer, operator),
        build_feedback_result(localizer, True, operator),
        build_feedback_result(localizer, False, operator),
    )

    for text in rendered:
        assert operator in text
        assert "&lt;a href=" not in text
        _assert_no_placeholders(text)
    assert punishment_label(localizer, "permanent_ban") in rendered[0]


@pytest.mark.parametrize("locale", _LOCALES)
def test_all_renderers_render_real_catalog_without_placeholders(locale: str) -> None:
    localizer = _localizer(locale)
    state = _state()
    rendered = (
        build_review_prompt(localizer, state, "Alice"),
        build_review_prompt(localizer, _state(recognized_text=None), "Alice"),
        build_review_ban_result(localizer, "Admin", "mute"),
        build_review_false_positive_result(localizer, "Admin"),
        build_review_ignore_result(localizer, "Admin"),
        build_immediate_processed(
            localizer,
            message_type=SpamMessageType.sticker,
            offender_mention="Alice",
            reason_codes=("rule:url",),
            confidence=0.8,
            punishment_key="mute",
            message_id=321,
        ),
        build_feedback_result(localizer, True, "Admin"),
        build_feedback_result(localizer, False, "Admin"),
    )

    for text in rendered:
        _assert_no_placeholders(text)
