"""antispam.py 配置类 handler i18n 测试(3c9)。

覆盖 cmd_antispam/cmd_antichannel/on_antispam_stats/on_antispam_retrain/
on_antispam_confirm_menu/on_antispam_confirm_toggle/on_antispam_back。
验证走 catalog,renderer 复用,callback 校验三件套,answer-before-edit_text。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam as handler
from src.services.spam_detector import RetrainCode, RetrainResult

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _localizer() -> MagicMock:
    loc = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}>" if not kw else f"<{key}:{kw}>"

    loc.t.side_effect = fake_t
    return loc


def _message(chat_type: str = "group") -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = CHAT_ID
    message.chat.title = "Test"
    message.from_user = MagicMock(id=42, username="admin")
    message.sender_chat = None
    message.answer = AsyncMock(return_value=MagicMock(message_id=1))
    return message


def _callback(data: str, chat_id: int = CHAT_ID, clicker_id: int = 42) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=clicker_id)
    cb.bot = AsyncMock()
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.chat.title = "Test"
    msg.edit_text = AsyncMock()
    cb.message = msg
    return cb


def _group(**kwargs) -> MagicMock:
    g = MagicMock()
    for k, v in kwargs.items():
        setattr(g, k, v)
    return g


# ===== cmd_antispam =====
async def test_cmd_antispam_private_rejected(mocker) -> None:
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    localizer = _localizer()
    message = _message(chat_type="private")

    await handler.cmd_antispam(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.antispam.error.group_only.message")


async def test_cmd_antispam_non_admin(mocker) -> None:
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=False))
    localizer = _localizer()
    message = _message()

    await handler.cmd_antispam(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.antispam.error.admin_only.message")


async def test_cmd_antispam_renders_menu_with_5_buttons(mocker) -> None:
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    localizer = _localizer()
    message = _message()

    await handler.cmd_antispam(message, AsyncMock(), localizer)

    # 主菜单 message + 5 按钮
    assert next(c for c in localizer.t.call_args_list if c.args == ("admin.antispam.menu.message",))
    for btn in ("enable", "disable", "stats", "retrain", "confirm"):
        assert next(
            c
            for c in localizer.t.call_args_list
            if c.args == (f"admin.antispam.menu.{btn}.button",)
        )


# ===== on_antispam_stats =====
async def test_stats_invalid_data(mocker) -> None:
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_antispam_stats(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_data.toast")


async def test_stats_chat_mismatch(mocker) -> None:
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"antispam_stats:{CHAT_ID}")
    cb.message.chat.id = CHAT_ID + 1

    await handler.on_antispam_stats(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_operation.toast")


async def test_stats_non_admin(mocker) -> None:
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    mocker.patch.object(handler.PermissionCache, "is_admin", new=AsyncMock(return_value=False))
    cb = _callback(data=f"antispam_stats:{CHAT_ID}", clicker_id=999)

    await handler.on_antispam_stats(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.stats.permission_denied.toast")


async def test_stats_success_escapes_numbers(mocker) -> None:
    """合法 → stats.message 注入 escape 后的数字 + classifier/embedder label。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    stats = {
        "total_samples": 100,
        "spam_samples": 30,
        "normal_samples": 70,
        "classifier_trained": True,
        "embedder_initialized": False,
    }
    detector = MagicMock()
    detector.get_statistics = AsyncMock(return_value=stats)
    mocker.patch.object(handler, "get_detector", return_value=detector)
    cb = _callback(data=f"antispam_stats:{CHAT_ID}")

    await handler.on_antispam_stats(cb, localizer)

    msg = next(c for c in localizer.t.call_args_list if c.args == ("admin.antispam.stats.message",))
    # 数字 escape_html(str(int)) = str(int) 本身(无 HTML 字符)
    assert msg.kwargs["total_samples"] == "100"
    assert msg.kwargs["spam_samples"] == "30"
    assert msg.kwargs["normal_samples"] == "70"
    # classifier trained → trained.label;embedder 未初始化 → uninitialized.label
    assert msg.kwargs["classifier_status"] == "<admin.stats.classifier.trained.label>"
    assert msg.kwargs["embedder_status"] == "<admin.stats.embedder.uninitialized.label>"
    cb.message.edit_text.assert_awaited_once()
    assert cb.answer.await_args.args[0] == "<admin.antispam.stats.updated.toast>"


# ===== on_antispam_retrain =====
async def test_retrain_non_super_admin(mocker) -> None:
    """非超级管理员 → retrain.permission_denied.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", set())
    cb = _callback(data=f"antispam_retrain:{CHAT_ID}", clicker_id=999)

    await handler.on_antispam_retrain(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.retrain.permission_denied.toast")


async def test_retrain_success_formats_accuracy_and_metrics(mocker) -> None:
    """success code → accuracy 格式化为百分比 + 4 个数字占位符。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    detector = MagicMock()
    detector.retrain_model = AsyncMock(
        return_value=RetrainResult(
            code=RetrainCode.success,
            params={
                "accuracy": 0.95,
                "total_samples": 100,
                "spam_samples": 30,
                "normal_samples": 70,
            },
        )
    )
    mocker.patch.object(handler, "get_detector", return_value=detector)
    cb = _callback(data=f"antispam_retrain:{CHAT_ID}")

    await handler.on_antispam_retrain(cb, localizer)

    # start.toast 先调用
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.antispam.retrain.start.toast",)
    )
    # result.success.message,accuracy → accuracy_percent "95.00%"
    result = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.antispam.retrain.result.success.message",)
    )
    assert result.kwargs["accuracy_percent"] == "95.00%"
    assert result.kwargs["total_samples"] == 100
    assert result.kwargs["spam_samples"] == 30
    assert result.kwargs["normal_samples"] == 70


async def test_retrain_failed_escapes_error(mocker) -> None:
    """failed code → error escape_html 注入。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    detector = MagicMock()
    detector.retrain_model = AsyncMock(
        return_value=RetrainResult(
            code=RetrainCode.failed,
            params={"error": "<script>x</script>"},
        )
    )
    mocker.patch.object(handler, "get_detector", return_value=detector)
    cb = _callback(data=f"antispam_retrain:{CHAT_ID}")

    await handler.on_antispam_retrain(cb, localizer)

    result = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.antispam.retrain.result.failed.message",)
    )
    assert result.kwargs["error"] == "&lt;script&gt;x&lt;/script&gt;"


async def test_retrain_insufficient_samples(mocker) -> None:
    """insufficient_samples code → {current}/{min_required} 占位符。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    detector = MagicMock()
    detector.retrain_model = AsyncMock(
        return_value=RetrainResult(
            code=RetrainCode.insufficient_samples,
            params={"current": 3, "min_required": 10},
        )
    )
    mocker.patch.object(handler, "get_detector", return_value=detector)
    cb = _callback(data=f"antispam_retrain:{CHAT_ID}")

    await handler.on_antispam_retrain(cb, localizer)

    result = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.antispam.retrain.result.insufficient_samples.message",)
    )
    assert result.kwargs["current"] == 3
    assert result.kwargs["min_required"] == 10


async def test_retrain_save_failed(mocker) -> None:
    """save_failed code → 无占位符。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    detector = MagicMock()
    detector.retrain_model = AsyncMock(
        return_value=RetrainResult(code=RetrainCode.save_failed, params={})
    )
    mocker.patch.object(handler, "get_detector", return_value=detector)
    cb = _callback(data=f"antispam_retrain:{CHAT_ID}")

    await handler.on_antispam_retrain(cb, localizer)

    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.antispam.retrain.result.save_failed.message",)
    )


# ===== on_antispam_confirm_menu =====
async def test_confirm_menu_invalid_data(mocker) -> None:
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_antispam_confirm_menu(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_data.toast")


async def test_confirm_menu_renders(mocker) -> None:
    """合法 → _render_antispam_confirm_menu(confirm.message + 双层 status)+ edit_text。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository,
        "get_or_create",
        new=AsyncMock(return_value=_group(spam_confirm_enabled=True)),
    )
    cb = _callback(data=f"antispam_confirm_menu:{CHAT_ID}")

    await handler.on_antispam_confirm_menu(cb, localizer)

    # confirm.message
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.antispam.confirm.message",)
    )
    # 双层 status
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.enabled.label",)
    )
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.groupset.status.enabled.label",)
    )
    cb.message.edit_text.assert_awaited_once()


# ===== on_antispam_confirm_toggle =====
async def test_confirm_toggle_invalid_action(mocker) -> None:
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"antispam_confirm_toggle:{CHAT_ID}:bogus")

    await handler.on_antispam_confirm_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_operation.toast")


async def test_confirm_toggle_update_failed(mocker) -> None:
    """update 返回 False → failed.toast。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository, "update_spam_confirm_settings", new=AsyncMock(return_value=False)
    )
    cb = _callback(data=f"antispam_confirm_toggle:{CHAT_ID}:on")

    await handler.on_antispam_confirm_toggle(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.failed.toast")


async def test_confirm_toggle_enable_answer_before_edit(mocker) -> None:
    """on → update(True) + confirm.callback.enabled.toast(answer 在 edit_text 前)。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    update = mocker.patch.object(
        handler.GroupRepository, "update_spam_confirm_settings", new=AsyncMock(return_value=True)
    )
    cb = _callback(data=f"antispam_confirm_toggle:{CHAT_ID}:on")

    await handler.on_antispam_confirm_toggle(cb, localizer)

    update.assert_awaited_once_with(CHAT_ID, True)
    # callback.answer 是 enabled.toast(show_alert=False)
    assert cb.answer.await_args.args[0] == "<admin.antispam.confirm.callback.enabled.toast>"
    assert cb.answer.await_args.kwargs.get("show_alert") is False
    cb.message.edit_text.assert_awaited_once()


async def test_confirm_toggle_edit_failure_keeps_success(mocker) -> None:
    """DB 更新成功后 edit_text 失败 → callback 仍是成功 toast(answer-before-edit_text)。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    mocker.patch.object(
        handler.GroupRepository, "update_spam_confirm_settings", new=AsyncMock(return_value=True)
    )
    cb = _callback(data=f"antispam_confirm_toggle:{CHAT_ID}:off")
    cb.message.edit_text = AsyncMock(side_effect=RuntimeError("message is not modified"))

    await handler.on_antispam_confirm_toggle(cb, localizer)

    assert cb.answer.await_args.args[0] == "<admin.antispam.confirm.callback.disabled.toast>"


# ===== on_antispam_back =====
async def test_back_invalid_data(mocker) -> None:
    localizer = _localizer()
    cb = _callback(data=None)

    await handler.on_antispam_back(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_data.toast")


async def test_back_chat_mismatch(mocker) -> None:
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"antispam_back:{CHAT_ID}")
    cb.message.chat.id = CHAT_ID + 1

    await handler.on_antispam_back(cb, localizer)

    localizer.t.assert_called_once_with("admin.antispam.callback.invalid_operation.toast")


async def test_back_renders_main_menu(mocker) -> None:
    """合法 → _render_antispam_menu + edit_text。"""
    localizer = _localizer()
    mocker.patch.object(handler.settings, "admin_ids", {42})
    cb = _callback(data=f"antispam_back:{CHAT_ID}")

    await handler.on_antispam_back(cb, localizer)

    assert next(c for c in localizer.t.call_args_list if c.args == ("admin.antispam.menu.message",))
    cb.message.edit_text.assert_awaited_once()


# ===== cmd_antichannel =====
async def test_cmd_antichannel_private_rejected(mocker) -> None:
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    localizer = _localizer()
    message = _message(chat_type="private")

    await handler.cmd_antichannel(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.antichannel.error.group_only.message")


async def test_cmd_antichannel_group_exists_disabled(mocker) -> None:
    """群组存在且禁用 → groupset.status.disabled 双层 + 复用 groupset antichannel message/button。"""
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    mocker.patch.object(
        handler.GroupRepository,
        "get",
        new=AsyncMock(return_value=_group(anti_channel_enabled=False)),
    )
    localizer = _localizer()
    message = _message()

    await handler.cmd_antichannel(message, AsyncMock(), localizer)

    # 复用 groupset antichannel message(含 status 占位符)
    msg = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.groupset.menu.antichannel.message",)
    )
    assert msg is not None
    # 双层 disabled status
    assert next(
        c for c in localizer.t.call_args_list if c.args == ("admin.common.status.disabled.label",)
    )


async def test_cmd_antichannel_no_group_uses_default_enabled(mocker) -> None:
    """群组不存在 → antichannel.status.default_enabled.label(含 enabled status)。"""
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    mocker.patch.object(handler.GroupRepository, "get", new=AsyncMock(return_value=None))
    localizer = _localizer()
    message = _message()

    await handler.cmd_antichannel(message, AsyncMock(), localizer)

    # default_enabled.label 注入 common enabled
    default = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.antichannel.status.default_enabled.label",)
    )
    assert default.kwargs["status"] == "<admin.common.status.enabled.label>"


async def test_cmd_antichannel_load_failure_uses_default(mocker) -> None:
    """GroupRepository.get 抛异常 → 回退 default_enabled.label。"""
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    mocker.patch.object(
        handler.GroupRepository, "get", new=AsyncMock(side_effect=RuntimeError("db"))
    )
    localizer = _localizer()
    message = _message()

    await handler.cmd_antichannel(message, AsyncMock(), localizer)

    assert next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("admin.antichannel.status.default_enabled.label",)
    )
