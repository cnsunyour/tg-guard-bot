"""cmd_set_verify + on_setverify_callback i18n 测试(3c7)。

验证 /setverify 命令与回调走 catalog(localizer.t 以正确 key 调用),
而非硬编码中文。13 验证方式按钮顺序/文案、白名单、权限、回显 common label。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _message(chat_type: str = "group", text: str = "/setverify") -> MagicMock:
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


def _patch_admin_check(mocker, is_admin: bool = True) -> None:
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=is_admin))
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())


# ===== cmd_set_verify 入口 =====
async def test_private_chat_rejected(mocker) -> None:
    """私聊 → group_only key。"""
    _patch_admin_check(mocker)
    localizer = _localizer()
    message = _message(chat_type="private")

    await handler.cmd_set_verify(message, AsyncMock(), _command(), localizer)

    localizer.t.assert_called_once_with("admin.setverify.error.group_only.message")


async def test_non_admin_rejected(mocker) -> None:
    """非管理员 → admin_only key。"""
    _patch_admin_check(mocker, is_admin=False)
    localizer = _localizer()
    message = _message()

    await handler.cmd_set_verify(message, AsyncMock(), _command(), localizer)

    localizer.t.assert_called_once_with("admin.setverify.error.admin_only.message")


async def test_prompt_and_13_buttons(mocker) -> None:
    """合法 → prompt key + 13 按钮 key 各调用一次,顺序与 _VERIFICATION_TYPES 一致。"""
    _patch_admin_check(mocker)
    localizer = _localizer()
    message = _message()
    bot = AsyncMock()

    await handler.cmd_set_verify(message, bot, _command(), localizer)

    # prompt 调用一次
    prompt_calls = [
        c for c in localizer.t.call_args_list if c.args == ("admin.setverify.prompt.message",)
    ]
    assert len(prompt_calls) == 1

    # 13 按钮 key,按 _VERIFICATION_TYPES 顺序
    button_calls = [
        c
        for c in localizer.t.call_args_list
        if c.args
        and c.args[0].startswith("admin.setverify.verification_type.")
        and c.args[0].endswith(".button")
    ]
    assert len(button_calls) == 13
    expected_keys = [
        f"admin.setverify.verification_type.{t}.button" for t in handler._VERIFICATION_TYPES
    ]
    actual_keys = [c.args[0] for c in button_calls]
    assert actual_keys == expected_keys


async def test_setverify_with_valid_arg_updates(mocker) -> None:
    """带合法方式 /setverify math → update_verification_type + result.saved(common label)。"""
    _patch_admin_check(mocker)
    localizer = _localizer()
    message = _message(text="/setverify math")
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock())
    update = mocker.patch.object(
        handler.GroupRepository, "update_verification_type", new=AsyncMock()
    )

    await handler.cmd_set_verify(message, AsyncMock(), _command(args="math"), localizer)

    update.assert_awaited_once_with(CHAT_ID, "math")
    saved = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.setverify.result.saved.message",)
    )
    assert saved.kwargs["verify_type"] == "<admin.common.verification_type.math.label>"


async def test_setverify_with_invalid_arg_shows_usage(mocker) -> None:
    """非法方式 /setverify bogus → usage.message,不更新。"""
    _patch_admin_check(mocker)
    localizer = _localizer()
    message = _message(text="/setverify bogus")
    update = mocker.patch.object(
        handler.GroupRepository, "update_verification_type", new=AsyncMock()
    )

    await handler.cmd_set_verify(message, AsyncMock(), _command(args="bogus"), localizer)

    localizer.t.assert_called_once_with("admin.setverify.usage.message")
    update.assert_not_awaited()


async def test_setverify_db_failure_shows_save_failed(mocker) -> None:
    """带参路径 DB 异常 → save_failed.toast,不崩溃。"""
    _patch_admin_check(mocker)
    localizer = _localizer()
    message = _message(text="/setverify math")
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(side_effect=RuntimeError("db"))
    )

    await handler.cmd_set_verify(message, AsyncMock(), _command(args="math"), localizer)

    assert message.answer.await_args.args[0] == "<admin.setverify.callback.save_failed.toast>"


# ===== on_setverify_callback =====
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


async def test_callback_missing_data(mocker) -> None:
    """callback.data 为空 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_setverify_callback(cb, localizer)

    localizer.t.assert_called_once_with("admin.setverify.callback.invalid_data.toast")
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_callback_valid_type_updates_and_renders_common_label(mocker) -> None:
    """合法类型 → update_verification_type 调用一次,成功消息用 common label 渲染。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    member = MagicMock(status="administrator")
    cb = _callback(data=f"setverify:{CHAT_ID}:math")
    cb.bot.get_chat_member = AsyncMock(return_value=member)
    update = mocker.patch.object(
        handler.GroupRepository, "update_verification_type", new=AsyncMock()
    )

    await handler.on_setverify_callback(cb, localizer)

    update.assert_awaited_once_with(CHAT_ID, "math")
    # 成功消息:common label(math)注入 result.saved
    saved = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.setverify.result.saved.message",)
    )
    assert saved.kwargs["verify_type"] == "<admin.common.verification_type.math.label>"
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited_once()


async def test_callback_invalid_type_rejected(mocker) -> None:
    """非法 verify_type → invalid_type.toast,不更新。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    member = MagicMock(status="administrator")
    cb = _callback(data=f"setverify:{CHAT_ID}:bogus")
    cb.bot.get_chat_member = AsyncMock(return_value=member)
    update = mocker.patch.object(
        handler.GroupRepository, "update_verification_type", new=AsyncMock()
    )

    await handler.on_setverify_callback(cb, localizer)

    toast = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.setverify.callback.invalid_type.toast",)
    )
    assert toast is not None
    update.assert_not_awaited()


async def test_callback_chat_mismatch_rejected(mocker) -> None:
    """callback.message.chat.id != callback_data chat_id → invalid_operation.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"setverify:{CHAT_ID}:math")
    cb.message.chat.id = CHAT_ID + 1  # 不一致

    await handler.on_setverify_callback(cb, localizer)

    localizer.t.assert_called_once_with("admin.setverify.callback.invalid_operation.toast")


async def test_callback_non_admin_in_group_rejected(mocker) -> None:
    """非群管理员且非 super admin → permission_denied.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    member = MagicMock(status="member")
    cb = _callback(data=f"setverify:{CHAT_ID}:math", clicker_id=999)
    cb.bot.get_chat_member = AsyncMock(return_value=member)

    await handler.on_setverify_callback(cb, localizer)

    localizer.t.assert_called_once_with("admin.setverify.callback.permission_denied.toast")


async def test_callback_permission_check_failed(mocker) -> None:
    """get_chat_member 抛异常 → permission_check_failed.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    cb = _callback(data=f"setverify:{CHAT_ID}:math", clicker_id=999)
    cb.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("net error"))

    await handler.on_setverify_callback(cb, localizer)

    localizer.t.assert_called_once_with("admin.setverify.callback.permission_check_failed.toast")


async def test_callback_exception_returns_save_failed(mocker) -> None:
    """update_verification_type 抛异常 → save_failed.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"setverify:{CHAT_ID}:math")
    mocker.patch.object(
        handler.GroupRepository,
        "update_verification_type",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )

    await handler.on_setverify_callback(cb, localizer)

    assert cb.answer.await_args.args[0] == "<admin.setverify.callback.save_failed.toast>"


async def test_callback_super_admin_skips_telegram_check(mocker) -> None:
    """super admin_id 命中 → 不调用 get_chat_member。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"setverify:{CHAT_ID}:math", clicker_id=42)
    mocker.patch.object(handler.GroupRepository, "update_verification_type", new=AsyncMock())

    await handler.on_setverify_callback(cb, localizer)

    cb.bot.get_chat_member.assert_not_awaited()
