"""cmd_groupset + on_groupset_menu + on_groupset_back i18n 测试(3c8)。

验证 /groupset 主菜单 + 6 子菜单 + 返回回调走 catalog,而非硬编码中文。
renderer 复用 3c7 的 _build_setverify_keyboard / _render_activity_panel。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _message(chat_type: str = "group") -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = CHAT_ID
    message.chat.title = "Test"
    message.text = "/groupset"
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


def _group(
    verification_type: str = "math",
    antispam: bool = True,
    antichannel: bool = False,
    activity: bool = True,
    timeout: int = 120,
    activity_skip_threshold: int = 50,
) -> MagicMock:
    g = MagicMock()
    g.verification_type = verification_type
    g.antispam_enabled = antispam
    g.anti_channel_enabled = antichannel
    g.activity_enabled = activity
    g.verification_timeout = timeout
    g.activity_skip_threshold = activity_skip_threshold
    return g


def _patch(mocker, is_admin: bool = True, group: MagicMock | None = None) -> MagicMock:
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=is_admin))
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(
        handler.GroupRepository,
        "get_or_create",
        new=AsyncMock(return_value=group or _group()),
    )
    return _localizer()


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


# ===== cmd_groupset 入口 =====
async def test_private_chat_rejected(mocker) -> None:
    """私聊 → group_only key。"""
    localizer = _patch(mocker)
    message = _message(chat_type="private")

    await handler.cmd_groupset(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.error.group_only.message")


async def test_non_admin_rejected(mocker) -> None:
    """非管理员 → admin_only key。"""
    localizer = _patch(mocker, is_admin=False)
    message = _message()

    await handler.cmd_groupset(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.error.admin_only.message")


async def test_load_failed_no_fake_state(mocker) -> None:
    """get_or_create 抛异常 → load_failed key,不伪造状态回退。"""
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(side_effect=RuntimeError("db"))
    )
    localizer = _localizer()
    message = _message()

    await handler.cmd_groupset(message, AsyncMock(), localizer)

    assert message.answer.await_args.args[0] == "<admin.groupset.error.load_failed.message>"


async def test_command_renders_main_menu_with_6_buttons(mocker) -> None:
    """合法 → main.message + 6 按钮 key 各一次。"""
    localizer = _patch(mocker, group=_group())
    message = _message()

    await handler.cmd_groupset(message, AsyncMock(), localizer)

    main = next(c for c in localizer.t.call_args_list if c.args == ("admin.groupset.main.message",))
    assert main is not None
    # 6 menu button key
    button_keys = [
        "admin.groupset.menu.verify.button",
        "admin.groupset.menu.timeout.button",
        "admin.groupset.menu.antispam.button",
        "admin.groupset.menu.antichannel.button",
        "admin.groupset.menu.activity.button",
        "admin.groupset.menu.activityskip.button",
    ]
    for k in button_keys:
        assert next(c for c in localizer.t.call_args_list if c.args == (k,))


# ===== _render_groupset_main_menu renderer =====
async def test_renderer_unknown_verification_type_falls_back(mocker) -> None:
    """verification_type 非白名单 → unknown.short.label。"""
    localizer = _localizer()
    group = _group(verification_type="bogus")

    _text, _kb = handler._render_groupset_main_menu(
        localizer,
        CHAT_ID,
        group.verification_type,
        antispam_enabled=True,
        antichannel_enabled=False,
        activity_enabled=True,
    )

    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.groupset.verification_type.unknown.short.label",)
    )


async def test_renderer_status_uses_common_injection(mocker) -> None:
    """antispam/antichannel/activity 三态用 common.status 注入 groupset.status。"""
    localizer = _localizer()

    handler._render_groupset_main_menu(
        localizer,
        CHAT_ID,
        "math",
        antispam_enabled=True,
        antichannel_enabled=False,
        activity_enabled=False,
    )

    # common.enabled + common.disabled 各出现（一个 enabled 两个 disabled）
    enabled_common = [
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.enabled.label",)
    ]
    disabled_common = [
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.disabled.label",)
    ]
    assert len(enabled_common) == 1
    assert len(disabled_common) == 2
    # groupset.status.enabled.label 注入 common status
    gset_enabled = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.groupset.status.enabled.label",)
    )
    assert gset_enabled.kwargs["status"] == "<admin.common.status.enabled.label>"


async def test_renderer_13_known_verify_types(mocker) -> None:
    """13 个白名单类型各自的 short.label key 命中(参数化)。"""
    localizer = _localizer()
    for vt in handler._VERIFICATION_TYPES:
        localizer.t.reset_mock()
        handler._render_groupset_main_menu(
            localizer,
            CHAT_ID,
            vt,
            antispam_enabled=True,
            antichannel_enabled=True,
            activity_enabled=True,
        )
        assert next(
            c
            for c in localizer.t.call_args_list
            if c.args == (f"admin.groupset.verification_type.{vt}.short.label",)
        ), f"missing short.label for {vt}"


# ===== on_groupset_menu =====
async def test_menu_callback_invalid_data(mocker) -> None:
    """callback.data 为空 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.invalid_data.toast")


async def test_menu_callback_invalid_format(mocker) -> None:
    """callback.data 分割后不足 3 段 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data="groupset_menu:abc")  # 只有 2 段

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.invalid_data.toast")


async def test_menu_callback_chat_mismatch(mocker) -> None:
    """message.chat.id != callback_data chat_id → invalid_operation.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"groupset_menu:{CHAT_ID}:verify")
    cb.message.chat.id = CHAT_ID + 1

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.invalid_operation.toast")


async def test_menu_callback_invalid_menu_type(mocker) -> None:
    """menu_type 非白名单 → invalid_operation.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"groupset_menu:{CHAT_ID}:bogus")

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.invalid_operation.toast")


async def test_menu_callback_non_admin_rejected(mocker) -> None:
    """非管理员 → permission_denied.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    mocker.patch.object(handler.PermissionCache, "is_admin", new=AsyncMock(return_value=False))
    cb = _callback(data=f"groupset_menu:{CHAT_ID}:verify", clicker_id=999)

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.permission_denied.toast")


async def test_menu_verify_branch_renders_13_plus_back(mocker) -> None:
    """verify 分支 → 13 setverify 按钮 key 各一次 + back.button 一次。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=_group())
    )
    cb = _callback(data=f"groupset_menu:{CHAT_ID}:verify")

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    # 13 setverify button key
    button_calls = [
        c
        for c in localizer.t.call_args_list
        if c.args
        and c.args[0].startswith("admin.setverify.verification_type.")
        and c.args[0].endswith(".button")
    ]
    assert len(button_calls) == 13
    # back 按钮
    assert next(c for c in localizer.t.call_args_list if c.args == ("admin.groupset.back.button",))
    # prompt
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.setverify.prompt.message",)
    )


async def test_menu_activity_branch_uses_panel_renderer(mocker) -> None:
    """activity 分支 → _render_activity_panel(panel.message + common status) + back。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=_group())
    )
    cb = _callback(data=f"groupset_menu:{CHAT_ID}:activity")

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    # panel.message 命中(来自 _render_activity_panel)
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activity.panel.message",)
    )
    # back 按钮
    assert next(c for c in localizer.t.call_args_list if c.args == ("admin.groupset.back.button",))


async def test_menu_activityskip_three_states(mocker) -> None:
    """activityskip 分支三态(http >0/=0/<0)threshold_source 正确。"""
    for global_thresh, expected_source_key in [
        (30, "admin.activityskip.source.global.label"),
        (0, "admin.activityskip.source.group.label"),
        (-1, "admin.activityskip.source.disabled.label"),
    ]:
        mocker.resetall()
        localizer = _localizer()
        mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
        mocker.patch.object(handler.settings, "admin_ids", {42})
        mocker.patch.object(handler.settings, "activity_skip_spam_check_threshold", global_thresh)
        mocker.patch.object(
            handler.GroupRepository,
            "get_or_create",
            new=AsyncMock(return_value=_group(activity_skip_threshold=50)),
        )
        cb = _callback(data=f"groupset_menu:{CHAT_ID}:activityskip")

        await handler.on_groupset_menu(cb, AsyncMock(), localizer)

        msg_call = next(
            c
            for c in localizer.t.call_args_list
            if c.args == ("admin.groupset.menu.activityskip.message",)
        )
        assert msg_call.kwargs["threshold_source"] == f"<{expected_source_key}>"


async def test_menu_success_calls_empty_answer(mocker) -> None:
    """成功路径 → callback.answer() 空回执(无参)。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"groupset_menu:{CHAT_ID}:verify")

    await handler.on_groupset_menu(cb, AsyncMock(), localizer)

    # 末尾 callback.answer() 无参调用
    cb.answer.assert_awaited()


# ===== on_groupset_back =====
async def test_back_callback_invalid_data(mocker) -> None:
    """back callback.data 为空 → invalid_data.toast。"""
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_groupset_back(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.invalid_data.toast")


async def test_back_callback_invalid_format(mocker) -> None:
    """back callback.data 不是 2 段 → invalid_data.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data="groupset_back:abc:extra")  # 3 段

    await handler.on_groupset_back(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.invalid_data.toast")


async def test_back_callback_chat_mismatch(mocker) -> None:
    """back message.chat.id != callback_data chat_id → invalid_operation.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"groupset_back:{CHAT_ID}")
    cb.message.chat.id = CHAT_ID + 1

    await handler.on_groupset_back(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.invalid_operation.toast")


async def test_back_callback_non_admin_rejected(mocker) -> None:
    """back 非管理员 → permission_denied.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    mocker.patch.object(handler.PermissionCache, "is_admin", new=AsyncMock(return_value=False))
    cb = _callback(data=f"groupset_back:{CHAT_ID}", clicker_id=999)

    await handler.on_groupset_back(cb, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.groupset.callback.permission_denied.toast")


async def test_back_renders_main_menu_and_edits(mocker) -> None:
    """back 合法 → _render_groupset_main_menu + edit_text。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=_group())
    )
    cb = _callback(data=f"groupset_back:{CHAT_ID}")

    await handler.on_groupset_back(cb, AsyncMock(), localizer)

    assert next(c for c in localizer.t.call_args_list if c.args == ("admin.groupset.main.message",))
    cb.message.edit_text.assert_awaited_once()


async def test_back_exception_returns_failed(mocker) -> None:
    """back 内部异常 → failed.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(side_effect=RuntimeError("db"))
    )
    cb = _callback(data=f"groupset_back:{CHAT_ID}")

    await handler.on_groupset_back(cb, AsyncMock(), localizer)

    assert cb.answer.await_args.args[0] == "<admin.groupset.callback.failed.toast>"
