"""cmd_activity + on_activity_callback i18n 测试(3c7)。

验证 /activity 命令与回调走 catalog(localizer.t 以正确 key 调用),
而非硬编码中文。面板 renderer 复用、enable/disable 分支、toast 全 i18n。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _message(chat_type: str = "group", text: str = "/activity") -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = CHAT_ID
    message.chat.title = "Test"
    message.text = text
    message.from_user = MagicMock(id=42)
    message.answer = AsyncMock(return_value=MagicMock())
    message.delete = AsyncMock()
    return message


def _localizer() -> MagicMock:
    loc = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}>" if not kw else f"<{key}:{kw}>"

    loc.t.side_effect = fake_t
    return loc


def _command(args: str | None = None) -> CommandObject:
    """构造 CommandObject(无参 args=None;带参 args="xxx")。"""
    return CommandObject(args=args)


def _patch(mocker, is_admin: bool = True, activity_enabled: bool = True) -> MagicMock:
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=is_admin))
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    group = MagicMock()
    group.activity_enabled = activity_enabled
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    mocker.patch.object(handler.GroupRepository, "update_activity_settings", new=AsyncMock())
    return group


def _callback(data: str, chat_id: int = CHAT_ID, clicker_id: int = 42) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=clicker_id)
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.edit_text = AsyncMock()
    cb.message = msg
    return cb


# ===== cmd_activity 入口 =====
async def test_private_chat_rejected(mocker) -> None:
    """私聊 → group_only key。"""
    _patch(mocker)
    localizer = _localizer()
    message = _message(chat_type="private")

    await handler.cmd_activity(message, AsyncMock(), _command(), localizer)

    localizer.t.assert_called_once_with("admin.activity.error.group_only.message")


async def test_non_admin_rejected(mocker) -> None:
    """非管理员 → admin_only key。"""
    _patch(mocker, is_admin=False)
    localizer = _localizer()
    message = _message()

    await handler.cmd_activity(message, AsyncMock(), _command(), localizer)

    localizer.t.assert_called_once_with("admin.activity.error.admin_only.message")


async def test_load_failed(mocker) -> None:
    """get_or_create 抛异常 → load_failed key。"""
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(side_effect=RuntimeError("db"))
    )
    localizer = _localizer()
    message = _message()

    await handler.cmd_activity(message, AsyncMock(), _command(), localizer)

    assert message.answer.await_args.args[0] == "<admin.activity.error.load_failed.message>"


async def test_command_renders_panel(mocker) -> None:
    """合法 → renderer panel.message + common status 双层注入。"""
    _patch(mocker, activity_enabled=True)
    localizer = _localizer()
    message = _message()

    await handler.cmd_activity(message, AsyncMock(), _command(), localizer)

    # common status 先调用,再注入 activity status
    common = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.enabled.label",)
    )
    assert common is not None
    activity_status = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activity.status.enabled.label",)
    )
    assert activity_status.kwargs["status"] == "<admin.common.status.enabled.label>"
    panel = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activity.panel.message",)
    )
    assert panel.kwargs["status"] == (
        "<admin.activity.status.enabled.label:{'status': '<admin.common.status.enabled.label>'}>"
    )


async def test_enable_with_arg_updates(mocker) -> None:
    """/activity on → update_activity_settings(True) + enabled.toast。"""
    _patch(mocker)
    localizer = _localizer()
    message = _message(text="/activity on")

    await handler.cmd_activity(message, AsyncMock(), _command(args="on"), localizer)

    handler.GroupRepository.update_activity_settings.assert_awaited_once_with(CHAT_ID, True)
    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.activity.callback.enabled.toast",)
    )


async def test_disable_with_arg_updates(mocker) -> None:
    """/activity off → update_activity_settings(False) + disabled.toast。"""
    _patch(mocker)
    localizer = _localizer()
    message = _message(text="/activity off")

    await handler.cmd_activity(message, AsyncMock(), _command(args="off"), localizer)

    handler.GroupRepository.update_activity_settings.assert_awaited_once_with(CHAT_ID, False)
    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.activity.callback.disabled.toast",)
    )


async def test_invalid_arg_shows_usage(mocker) -> None:
    """/activity bogus → usage.message,不更新。"""
    _patch(mocker)
    localizer = _localizer()
    message = _message(text="/activity bogus")

    await handler.cmd_activity(message, AsyncMock(), _command(args="bogus"), localizer)

    localizer.t.assert_called_once_with("admin.activity.usage.message")
    handler.GroupRepository.update_activity_settings.assert_not_awaited()


# ===== _render_activity_panel renderer =====
async def test_renderer_enabled(mocker) -> None:
    """enabled=True → status 用 enabled label + enable/disable button 各一次。"""
    localizer = _localizer()

    text, _kb = handler._render_activity_panel(localizer, CHAT_ID, enabled=True)

    assert "<admin.activity.panel.message:" in text
    # 两个按钮 key
    enable_calls = [
        c for c in localizer.t.call_args_list if c.args == ("admin.activity.enable.button",)
    ]
    disable_calls = [
        c for c in localizer.t.call_args_list if c.args == ("admin.activity.disable.button",)
    ]
    assert len(enable_calls) == 1
    assert len(disable_calls) == 1


async def test_renderer_disabled(mocker) -> None:
    """enabled=False → status 用 disabled label。"""
    localizer = _localizer()

    handler._render_activity_panel(localizer, CHAT_ID, enabled=False)

    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.disabled.label",)
    )
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activity.status.disabled.label",)
    )


# ===== on_activity_callback =====
async def test_callback_missing_data(mocker) -> None:
    """callback.data 为空 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.activity.callback.invalid_data.toast")


async def test_callback_chat_mismatch_rejected(mocker) -> None:
    """chat.id != callback_data chat_id → invalid_operation.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"activity:{CHAT_ID}:enable")
    cb.message.chat.id = CHAT_ID + 1

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.activity.callback.invalid_operation.toast")


async def test_callback_invalid_action_rejected(mocker) -> None:
    """action 非 enable/disable → invalid_operation.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"activity:{CHAT_ID}:bogus")

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.activity.callback.invalid_operation.toast")


async def test_callback_non_admin_rejected(mocker) -> None:
    """非管理员 → permission_denied.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    mocker.patch.object(handler.PermissionCache, "is_admin", new=AsyncMock(return_value=False))
    cb = _callback(data=f"activity:{CHAT_ID}:enable", clicker_id=999)

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.activity.callback.permission_denied.toast")


async def test_callback_enable_when_disabled(mocker) -> None:
    """禁用→启用 → update_activity_settings(True) + enabled.toast。"""
    _patch(mocker, activity_enabled=False)
    mocker.patch.object(handler.settings, "admin_ids", {42})
    localizer = _localizer()
    cb = _callback(data=f"activity:{CHAT_ID}:enable", clicker_id=42)

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    handler.GroupRepository.update_activity_settings.assert_awaited_once_with(CHAT_ID, True)
    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.activity.callback.enabled.toast",)
    )


async def test_callback_enable_when_already_enabled(mocker) -> None:
    """已启用 → already_enabled.toast,不更新。"""
    _patch(mocker, activity_enabled=True)
    mocker.patch.object(handler.settings, "admin_ids", {42})
    localizer = _localizer()
    cb = _callback(data=f"activity:{CHAT_ID}:enable", clicker_id=42)

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    handler.GroupRepository.update_activity_settings.assert_not_awaited()
    localizer.t.assert_called_once_with("admin.activity.callback.already_enabled.toast")


async def test_callback_disable_when_enabled(mocker) -> None:
    """启用→禁用 → update_activity_settings(False) + disabled.toast。"""
    _patch(mocker, activity_enabled=True)
    mocker.patch.object(handler.settings, "admin_ids", {42})
    localizer = _localizer()
    cb = _callback(data=f"activity:{CHAT_ID}:disable", clicker_id=42)

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    handler.GroupRepository.update_activity_settings.assert_awaited_once_with(CHAT_ID, False)
    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.activity.callback.disabled.toast",)
    )


async def test_callback_disable_when_already_disabled(mocker) -> None:
    """已禁用 → already_disabled.toast,不更新。"""
    _patch(mocker, activity_enabled=False)
    mocker.patch.object(handler.settings, "admin_ids", {42})
    localizer = _localizer()
    cb = _callback(data=f"activity:{CHAT_ID}:disable", clicker_id=42)

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    handler.GroupRepository.update_activity_settings.assert_not_awaited()
    localizer.t.assert_called_once_with("admin.activity.callback.already_disabled.toast")


async def test_callback_exception_returns_failed(mocker) -> None:
    """内部异常 → failed.toast。"""
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(side_effect=RuntimeError("db"))
    )
    localizer = _localizer()
    cb = _callback(data=f"activity:{CHAT_ID}:enable", clicker_id=42)

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    assert cb.answer.await_args.args[0] == "<admin.activity.callback.failed.toast>"


async def test_callback_value_error_returns_invalid_data(mocker) -> None:
    """callback_data 无法解析 → invalid_data.toast(ValueError 分支)。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    # chat_id 部分 non-int → int() 抛 ValueError
    cb = _callback(data="activity:abc:enable")

    await handler.on_activity_callback(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.activity.callback.invalid_data.toast")
