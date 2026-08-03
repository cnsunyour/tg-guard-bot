"""cmd_activity_skip i18n 测试（3c6）。

验证 /activityskip + _show_activity_skip_config 走 catalog（localizer.t 以正确 key 调用）,
而非硬编码中文。同时回归两个既有 bug:
1. update_activity_skip_threshold 恰好调用一次（不再重复两次）
2. stale group 同步新阈值（报告 group_threshold/有效阈值 显示新值而非旧值）
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _message(chat_type: str = "group", text: str = "/activityskip 50") -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = CHAT_ID
    message.chat.title = "Test"
    message.text = text
    message.from_user = MagicMock(id=42)
    message.answer = AsyncMock(return_value=MagicMock())
    message.delete = AsyncMock()
    return message


def _group(threshold: int = 10) -> MagicMock:
    g = MagicMock()
    g.activity_skip_threshold = threshold
    return g


def _patch(mocker, is_admin: bool = True, group_threshold: int = 10) -> MagicMock:
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=is_admin))
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(
        handler.GroupRepository,
        "get_or_create",
        new=AsyncMock(return_value=_group(group_threshold)),
    )
    mocker.patch.object(handler.GroupRepository, "update_activity_skip_threshold", new=AsyncMock())
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}>" if not kw else f"<{key}:{kw}>"
    return localizer


# ===== cmd_activity_skip 入口 =====
async def test_from_user_none_returns(mocker) -> None:
    """from_user is None → 直接返回,不访问 repository。"""
    localizer = _patch(mocker)
    message = _message()
    message.from_user = None

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    localizer.t.assert_not_called()
    handler.GroupRepository.get_or_create.assert_not_awaited()


async def test_private_chat_rejected(mocker) -> None:
    """私聊 → group_only key,不检查权限、不访问 repository。"""
    localizer = _patch(mocker)
    message = _message(chat_type="private")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.activityskip.error.group_only.message")
    handler.check_admin_permission.assert_not_awaited()
    handler.GroupRepository.get_or_create.assert_not_awaited()


async def test_non_admin_rejected(mocker) -> None:
    """非管理员 → admin_only key。"""
    localizer = _patch(mocker, is_admin=False)
    message = _message()

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.activityskip.error.admin_only.message")


async def test_validation_not_integer(mocker) -> None:
    """非数字 → not_integer key,不更新数据库。"""
    localizer = _patch(mocker)
    message = _message(text="/activityskip abc")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.activityskip.validation.not_integer.message",)
    handler.GroupRepository.update_activity_skip_threshold.assert_not_awaited()


async def test_validation_negative(mocker) -> None:
    """负数 → negative key,不更新数据库。"""
    localizer = _patch(mocker)
    message = _message(text="/activityskip -5")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.activityskip.validation.negative.message",)
    handler.GroupRepository.update_activity_skip_threshold.assert_not_awaited()


# ===== bug#1 回归:不再重复两次 update =====
async def test_update_called_exactly_once(mocker) -> None:
    """回归 bug#1: update_activity_skip_threshold 恰好调用一次（原代码调用两次）。"""
    localizer = _patch(mocker)
    message = _message(text="/activityskip 50")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    handler.GroupRepository.update_activity_skip_threshold.assert_awaited_once_with(CHAT_ID, 50)


# ===== bug#2 回归:stale group 同步新阈值 =====
async def test_stale_group_synced_to_new_threshold(mocker) -> None:
    """回归 bug#2: 设置后 group.activity_skip_threshold 同步为新值,报告显示新阈值。

    初始 group.activity_skip_threshold=10,执行 /activityskip 42。
    报告占位符 group_threshold 与(全局=0 时)effective_threshold 都必须为 42。
    """
    localizer = _patch(mocker, group_threshold=10)
    message = _message(text="/activityskip 42")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    report_call = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activityskip.report.message",)
    )
    assert report_call.kwargs["group_threshold"] == 42


# ===== 三态全局覆盖 =====
async def test_global_threshold_uniform_override(mocker) -> None:
    """全局 >0 → effective = global_threshold,源=global,warning=global_override,gmode=uniform。"""
    localizer = _patch(mocker, group_threshold=10)
    mocker.patch.object(handler.settings, "activity_skip_spam_check_threshold", 30)
    message = _message(text="/activityskip")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    report = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activityskip.report.message",)
    )
    assert report.kwargs["effective_threshold"] == 30
    assert report.kwargs["global_mode"] == "<admin.activityskip.global_mode.uniform.label>"
    assert report.kwargs["threshold_source"] == "<admin.activityskip.source.global.label>"
    assert report.kwargs["warning_block"] == (
        "<admin.activityskip.warning.global_override.message:{'global_threshold': 30}>\n\n"
    )


async def test_global_threshold_zero_uses_group(mocker) -> None:
    """全局 =0 → effective = group_threshold,源=group,无 warning,gmode=group。"""
    localizer = _patch(mocker, group_threshold=15)
    mocker.patch.object(handler.settings, "activity_skip_spam_check_threshold", 0)
    message = _message(text="/activityskip")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    report = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activityskip.report.message",)
    )
    assert report.kwargs["effective_threshold"] == 15
    assert report.kwargs["group_threshold"] == 15
    assert report.kwargs["global_mode"] == "<admin.activityskip.global_mode.group.label>"
    assert report.kwargs["threshold_source"] == "<admin.activityskip.source.group.label>"
    assert report.kwargs["warning_block"] == ""


async def test_global_threshold_negative_disabled(mocker) -> None:
    """全局 <0 → effective = 0,源=disabled,warning=globally_disabled,gmode=disabled。"""
    localizer = _patch(mocker, group_threshold=10)
    mocker.patch.object(handler.settings, "activity_skip_spam_check_threshold", -1)
    message = _message(text="/activityskip")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    report = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activityskip.report.message",)
    )
    assert report.kwargs["effective_threshold"] == 0
    assert report.kwargs["global_mode"] == "<admin.activityskip.global_mode.disabled.label>"
    assert report.kwargs["threshold_source"] == "<admin.activityskip.source.disabled.label>"
    assert report.kwargs["warning_block"] == (
        "<admin.activityskip.warning.globally_disabled.message>\n\n"
    )


async def test_success_block_rendered_after_setting(mocker) -> None:
    """设置成功 → success_block 含 result.saved(新值),非空字符串。"""
    localizer = _patch(mocker, group_threshold=10)
    message = _message(text="/activityskip 50")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    report = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activityskip.report.message",)
    )
    assert report.kwargs["success_block"] == (
        "<admin.activityskip.result.saved.message:{'new_value': 50}>\n\n"
    )


async def test_view_mode_no_success_block(mocker) -> None:
    """查看模式 → success_block 为空字符串(不传 new_value)。"""
    localizer = _patch(mocker, group_threshold=15)
    mocker.patch.object(handler.settings, "activity_skip_spam_check_threshold", 0)
    message = _message(text="/activityskip")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    report = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activityskip.report.message",)
    )
    assert report.kwargs["success_block"] == ""
    handler.GroupRepository.update_activity_skip_threshold.assert_not_awaited()


async def test_report_receives_all_seven_placeholders(mocker) -> None:
    """report.message 必须收到全部 7 个占位符。"""
    localizer = _patch(mocker, group_threshold=10)
    mocker.patch.object(handler.settings, "activity_skip_spam_check_threshold", 0)
    message = _message(text="/activityskip")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    report = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.activityskip.report.message",)
    )
    expected = {
        "success_block",
        "global_threshold",
        "global_mode",
        "group_threshold",
        "effective_threshold",
        "threshold_source",
        "warning_block",
    }
    assert set(report.kwargs) == expected


# ===== 异常分支 =====
async def test_update_failure_returns_failed_key(mocker) -> None:
    """update 抛异常 → failed key,不渲染报告。"""
    localizer = _patch(mocker)
    mocker.patch.object(
        handler.GroupRepository,
        "update_activity_skip_threshold",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )
    message = _message(text="/activityskip 50")

    await handler.cmd_activity_skip(message, AsyncMock(), localizer)

    assert message.answer.await_args.args[0] == "<admin.activityskip.error.failed.message>"
