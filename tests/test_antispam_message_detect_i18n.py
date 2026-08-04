"""antispam.py 消息检测 handler 遗漏文案 i18n 测试(3c10)。

覆盖 check_and_handle_channel_as_sender 频道马甲警告(2 处)+
notify_activity_restriction 活跃度限制私聊通知。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam as handler

pytestmark = pytest.mark.unit


def _localizer() -> MagicMock:
    loc = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}>" if not kw else f"<{key}:{kw}>"

    loc.t.side_effect = fake_t
    return loc


def _message(*, has_user: bool = True, channel_title: str | None = "SpamChan") -> MagicMock:
    message = MagicMock()
    message.chat.id = -100
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    if has_user:
        message.from_user = MagicMock(id=42, first_name="User", username="user")
    else:
        message.from_user = None
    # is_channel_as_sender: sender_chat 非 None 且 != chat.id 且 type=channel
    message.sender_chat = MagicMock()
    message.sender_chat.id = -200
    message.sender_chat.type = "channel"
    message.sender_chat.title = channel_title
    return message


# ===== check_and_handle_channel_as_sender: 频道马甲警告 =====
async def test_channel_impersonation_user_warning_uses_catalog(mocker) -> None:
    """有用户 → warning.user.message,{user}/{channel} 注入,channel escape。"""
    localizer = _localizer()
    bot = AsyncMock(id=999)
    message = _message(has_user=True, channel_title="<x>&Co")

    mocker.patch.object(handler, "is_channel_as_sender", return_value=True)
    mocker.patch.object(handler, "should_skip_sender", return_value=False)
    mocker.patch.object(handler.GroupRepository, "get", new=AsyncMock(return_value=None))
    mocker.patch.object(handler, "get_resolver")
    handler.get_resolver.return_value.for_group = AsyncMock(return_value="zh-Hans")
    mocker.patch.object(
        handler,
        "get_translator",
        return_value=MagicMock(for_locale=MagicMock(return_value=localizer)),
    )
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    warn_user = mocker.patch.object(handler.ModerationService, "warn_user", new=AsyncMock())

    result = await handler.check_and_handle_channel_as_sender(message, bot)

    assert result is True
    # warning.user.message 调用
    warn_call = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("antispam.channel_impersonation.warning.user.message",)
    )
    assert warn_call.kwargs["channel"] == "&lt;x&gt;&amp;Co"  # escape_html
    # warn_user 记录
    warn_user.assert_awaited_once()
    # auto_delete 调用
    handler.auto_delete_message.assert_awaited()


async def test_channel_impersonation_anonymous_warning_uses_catalog(mocker) -> None:
    """无用户(匿名) → warning.anonymous.message,仅 {channel}。"""
    localizer = _localizer()
    bot = AsyncMock(id=999)
    message = _message(has_user=False, channel_title="AnonChan")

    mocker.patch.object(handler, "is_channel_as_sender", return_value=True)
    mocker.patch.object(handler, "should_skip_sender", return_value=False)
    mocker.patch.object(handler.GroupRepository, "get", new=AsyncMock(return_value=None))
    mocker.patch.object(handler, "get_resolver")
    handler.get_resolver.return_value.for_group = AsyncMock(return_value="zh-Hans")
    mocker.patch.object(
        handler,
        "get_translator",
        return_value=MagicMock(for_locale=MagicMock(return_value=localizer)),
    )
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())

    result = await handler.check_and_handle_channel_as_sender(message, bot)

    assert result is True
    anon_call = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("antispam.channel_impersonation.warning.anonymous.message",)
    )
    assert anon_call.kwargs["channel"] == "AnonChan"
    # 无用户 → 不调用 warn_user(无 from_user)


async def test_channel_impersonation_no_title_uses_unknown_label(mocker) -> None:
    """sender_chat.title 为 None → unknown_channel.label 占位。"""
    localizer = _localizer()
    bot = AsyncMock(id=999)
    message = _message(has_user=False, channel_title=None)

    mocker.patch.object(handler, "is_channel_as_sender", return_value=True)
    mocker.patch.object(handler, "should_skip_sender", return_value=False)
    mocker.patch.object(handler.GroupRepository, "get", new=AsyncMock(return_value=None))
    mocker.patch.object(handler, "get_resolver")
    handler.get_resolver.return_value.for_group = AsyncMock(return_value="zh-Hans")
    mocker.patch.object(
        handler,
        "get_translator",
        return_value=MagicMock(for_locale=MagicMock(return_value=localizer)),
    )
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())

    await handler.check_and_handle_channel_as_sender(message, bot)

    # unknown_channel.label 调用
    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("antispam.channel_impersonation.unknown_channel.label",)
    )
    # anonymous warning 的 channel 占位为 escape 后的 unknown label
    # (真实 label 无 HTML 字符 escape 不变;mock 的 <key> 含 <> 被 escape)
    anon_call = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("antispam.channel_impersonation.warning.anonymous.message",)
    )
    assert (
        anon_call.kwargs["channel"]
        == "&lt;antispam.channel_impersonation.unknown_channel.label&gt;"
    )


async def test_channel_impersonation_not_channel_returns_false(mocker) -> None:
    """非频道马甲消息 → 直接返回 False,不解析 locale。"""
    mocker.patch.object(handler, "is_channel_as_sender", return_value=False)
    mocker.patch.object(handler, "get_resolver")
    bot = AsyncMock()
    message = _message()

    result = await handler.check_and_handle_channel_as_sender(message, bot)

    assert result is False
    handler.get_resolver.assert_not_called()


# ===== notify_activity_restriction =====
async def test_notify_activity_restriction_uses_private_from_group(mocker) -> None:
    """私聊通知用 for_private_from_group(用户偏好优先),parse_mode HTML。"""
    localizer = _localizer()
    bot = AsyncMock()
    resolver = MagicMock()
    resolver.for_private_from_group = AsyncMock(return_value="zh-Hant")
    mocker.patch.object(handler, "get_resolver", return_value=resolver)
    mocker.patch.object(
        handler,
        "get_translator",
        return_value=MagicMock(for_locale=MagicMock(return_value=localizer)),
    )

    await handler.notify_activity_restriction(
        bot, user_id=42, current_activity=0, group_chat_id=-100
    )

    # for_private_from_group 用 user_id + group_chat_id
    resolver.for_private_from_group.assert_awaited_once_with(user_id=42, group_chat_id=-100)
    # activity_restriction.private.message 注入 activity
    msg_call = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("antispam.activity_restriction.private.message",)
    )
    assert msg_call.kwargs["activity"] == 0
    # send_message parse_mode HTML
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs.get("parse_mode") == "HTML"
