"""send_group_welcome mention 样式测试（3c1-5）。

验证 plain/linked mention 样式正确渲染（均经脱敏）+ parse_mode="HTML"，
供 handle_verification_success / on_captcha_text_input / on_webapp_data 复用。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import verification as handler

pytestmark = pytest.mark.unit


def _patch_i18n(mocker) -> MagicMock:
    resolver = AsyncMock()
    resolver.for_group.return_value = "zh-Hans"
    mocker.patch.object(handler, "get_resolver", return_value=resolver)

    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"[{key}] user={kw.get('user', '')}"
    translator = MagicMock()
    translator.for_locale.return_value = localizer
    mocker.patch.object(handler, "get_translator", return_value=translator)
    return localizer


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 42
    user.full_name = "Alice"
    user.first_name = "Alice"
    user.username = "alice"
    return user


async def test_plain_mention_default_style(mocker) -> None:
    """默认 plain 样式：format_user_mention 纯文本脱敏（无 <a> 标签），parse_mode=HTML。"""
    _patch_i18n(mocker)
    bot = AsyncMock()

    await handler.send_group_welcome(bot, -100, _user())

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<a href=" not in kwargs["text"]


async def test_linked_mention_masked_clickable(mocker) -> None:
    """linked 样式：masked_mention_html 可点击 <a> 链接（脱敏名），管理员可点击定位。"""
    _patch_i18n(mocker)
    bot = AsyncMock()

    await handler.send_group_welcome(bot, -100, _user(), mention_style="linked")

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert '<a href="tg://user?id=42">' in kwargs["text"]
