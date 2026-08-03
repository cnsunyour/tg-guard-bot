"""on_antispam_toggle + on_antichannel_toggle i18n 测试(3c8 遗留 TODO 收尾)。

验证 groupset 跳转的 2 toggle 回调走 catalog,而非硬编码中文。
on_antichannel edit_text 复用 admin.groupset.menu.antichannel.message(消除重复长说明)。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _localizer() -> MagicMock:
    loc = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}>" if not kw else f"<{key}:{kw}>"

    loc.t.side_effect = fake_t
    return loc


def _callback(data: str, chat_id: int = CHAT_ID, clicker_id: int = 42) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=clicker_id)
    cb.bot = AsyncMock()
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.edit_text = AsyncMock()
    cb.message = msg
    return cb


# ===== on_antispam_toggle =====
async def test_antispam_missing_data(mocker) -> None:
    """callback.data 为空 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_antispam_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_data.toast")


async def test_antispam_invalid_format(mocker) -> None:
    """callback.data 非 3 段 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data="antispam_toggle:abc")  # 2 段

    await handler.on_antispam_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_data.toast")


async def test_antispam_chat_mismatch(mocker) -> None:
    """chat.id != callback_data chat_id → invalid_operation.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"antispam_toggle:{CHAT_ID}:on")
    cb.message.chat.id = CHAT_ID + 1

    await handler.on_antispam_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_operation.toast")


async def test_antispam_invalid_action(mocker) -> None:
    """action 非 on/off → invalid_operation.toast,不更新。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"antispam_toggle:{CHAT_ID}:bogus")
    update = mocker.patch.object(
        handler.GroupRepository, "update_antispam_settings", new=AsyncMock()
    )

    await handler.on_antispam_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_operation.toast")
    update.assert_not_awaited()


async def test_antispam_non_admin_rejected(mocker) -> None:
    """非管理员 → permission_denied.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    mocker.patch.object(handler.PermissionCache, "is_admin", new=AsyncMock(return_value=False))
    cb = _callback(data=f"antispam_toggle:{CHAT_ID}:on", clicker_id=999)

    await handler.on_antispam_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.permission_denied.toast")


async def test_antispam_enable_uses_common_status_and_result(mocker) -> None:
    """on → update(True) + result.message(common status 注入) + enabled.toast(show_alert=False)。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    update = mocker.patch.object(
        handler.GroupRepository, "update_antispam_settings", new=AsyncMock()
    )
    cb = _callback(data=f"antispam_toggle:{CHAT_ID}:on")

    await handler.on_antispam_toggle(cb, localizer)

    update.assert_awaited_once_with(CHAT_ID, True)
    # result.message 用 common status 注入
    result = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.antispam.result.message",)
    )
    assert result.kwargs["status"] == "<admin.common.status.enabled.label>"
    # toast show_alert=False
    toast = cb.answer.await_args
    assert toast.args[0] == "<admin.antispam.callback.enabled.toast>"
    assert toast.kwargs.get("show_alert") is False


async def test_antispam_disable(mocker) -> None:
    """off → update(False) + disabled.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    update = mocker.patch.object(
        handler.GroupRepository, "update_antispam_settings", new=AsyncMock()
    )
    cb = _callback(data=f"antispam_toggle:{CHAT_ID}:off")

    await handler.on_antispam_toggle(cb, localizer)

    update.assert_awaited_once_with(CHAT_ID, False)
    assert cb.answer.await_args.args[0] == "<admin.antispam.callback.disabled.toast>"


async def test_antispam_exception_returns_failed(mocker) -> None:
    """update 抛异常 → failed.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository,
        "update_antispam_settings",
        new=AsyncMock(side_effect=RuntimeError("db")),
    )
    cb = _callback(data=f"antispam_toggle:{CHAT_ID}:on")

    await handler.on_antispam_toggle(cb, localizer)

    assert cb.answer.await_args.args[0] == "<admin.antispam.callback.failed.toast>"


async def test_antispam_edit_text_failure_keeps_success_toast(mocker) -> None:
    """DB 更新成功后 edit_text 失败 → callback 仍为成功 toast(不误报 failed)。

    回归 codex P2:callback.answer 在 edit_text 之前,DB 已持久化即告知用户成功。
    """
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(handler.GroupRepository, "update_antispam_settings", new=AsyncMock())
    cb = _callback(data=f"antispam_toggle:{CHAT_ID}:on")
    cb.message.edit_text = AsyncMock(side_effect=RuntimeError("message is not modified"))

    await handler.on_antispam_toggle(cb, localizer)

    # callback 仍是成功 toast(edit_text 失败被吞,记 warning)
    assert cb.answer.await_args.args[0] == "<admin.antispam.callback.enabled.toast>"


async def test_antispam_value_error(mocker) -> None:
    """chat_id non-int → invalid_data.toast(ValueError 分支)。"""
    localizer = _localizer()
    cb = _callback(data="antispam_toggle:abc:on")

    await handler.on_antispam_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_data.toast")


# ===== on_antichannel_toggle =====
async def test_antichannel_missing_data(mocker) -> None:
    """callback.data 为空 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_antichannel_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antichannel.callback.invalid_data.toast")


async def test_antichannel_invalid_action(mocker) -> None:
    """action 非 on/off → invalid_operation.toast,不更新。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"antichannel_toggle:{CHAT_ID}:bogus")
    update = mocker.patch.object(
        handler.GroupRepository, "update_antichannel_settings", new=AsyncMock()
    )
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock())

    await handler.on_antichannel_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antichannel.callback.invalid_operation.toast")
    update.assert_not_awaited()


async def test_antichannel_non_admin_rejected(mocker) -> None:
    """非管理员 → permission_denied.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    mocker.patch.object(handler.PermissionCache, "is_admin", new=AsyncMock(return_value=False))
    cb = _callback(data=f"antichannel_toggle:{CHAT_ID}:on", clicker_id=999)

    await handler.on_antichannel_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antichannel.callback.permission_denied.toast")


async def test_antichannel_enable_reuses_groupset_message(mocker) -> None:
    """on → update(True) + edit_text 复用 groupset.menu.antichannel.message(common→groupset.status 双层注入)。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    group = MagicMock()
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    update = mocker.patch.object(
        handler.GroupRepository, "update_antichannel_settings", new=AsyncMock()
    )
    cb = _callback(data=f"antichannel_toggle:{CHAT_ID}:on")

    await handler.on_antichannel_toggle(cb, localizer)

    update.assert_awaited_once_with(CHAT_ID, True)
    # common status 先调用
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.enabled.label",)
    )
    # groupset.status.enabled.label 注入 common
    gset_status = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.groupset.status.enabled.label",)
    )
    assert gset_status.kwargs["status"] == "<admin.common.status.enabled.label>"
    # edit_text 用 groupset.menu.antichannel.message
    menu = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.groupset.menu.antichannel.message",)
    )
    assert menu is not None
    # toast show_alert=False
    assert cb.answer.await_args.args[0] == "<admin.antichannel.callback.enabled.toast>"
    assert cb.answer.await_args.kwargs.get("show_alert") is False


async def test_antichannel_disable(mocker) -> None:
    """off → update(False) + disabled.toast + disabled status label。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    group = MagicMock()
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    update = mocker.patch.object(
        handler.GroupRepository, "update_antichannel_settings", new=AsyncMock()
    )
    cb = _callback(data=f"antichannel_toggle:{CHAT_ID}:off")

    await handler.on_antichannel_toggle(cb, localizer)

    update.assert_awaited_once_with(CHAT_ID, False)
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.disabled.label",)
    )
    assert cb.answer.await_args.args[0] == "<admin.antichannel.callback.disabled.toast>"


async def test_antichannel_exception_returns_failed(mocker) -> None:
    """update 抛异常 → failed.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    group = MagicMock()
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    mocker.patch.object(
        handler.GroupRepository,
        "update_antichannel_settings",
        new=AsyncMock(side_effect=RuntimeError("db")),
    )
    cb = _callback(data=f"antichannel_toggle:{CHAT_ID}:on")

    await handler.on_antichannel_toggle(cb, localizer)

    assert cb.answer.await_args.args[0] == "<admin.antichannel.callback.failed.toast>"


async def test_antichannel_edit_text_failure_keeps_success_toast(mocker) -> None:
    """DB 更新成功后 edit_text 失败 → callback 仍为成功 toast(不误报 failed)。

    回归 codex P2:callback.answer 在 edit_text 之前,DB 已持久化即告知用户成功。
    """
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    group = MagicMock()
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    mocker.patch.object(handler.GroupRepository, "update_antichannel_settings", new=AsyncMock())
    cb = _callback(data=f"antichannel_toggle:{CHAT_ID}:on")
    cb.message.edit_text = AsyncMock(side_effect=RuntimeError("message is not modified"))

    await handler.on_antichannel_toggle(cb, localizer)

    assert cb.answer.await_args.args[0] == "<admin.antichannel.callback.enabled.toast>"


async def test_antichannel_value_error(mocker) -> None:
    """chat_id non-int → invalid_data.toast(ValueError 分支)。"""
    localizer = _localizer()
    cb = _callback(data="antichannel_toggle:abc:on")

    await handler.on_antichannel_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antichannel.callback.invalid_data.toast")
