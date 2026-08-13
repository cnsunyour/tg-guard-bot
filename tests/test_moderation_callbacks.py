"""举报按钮回调契约测试

覆盖：
- approve/reject/ignore 三个回调必须严格校验实际点击者 ``callback.from_user``
  （而非 Bot 发送的 ``callback.message``——其 from_user 是 Bot 自身，曾导致任意
  成员可绕过权限执行封禁/拒绝）
- 处理完成后（成功/业务失败/异常）必须 edit_text 移除按钮 + 安排 30s 删除，
  杜绝提示残留（对齐 on_spam_review_callback 的清理契约）
- 前置校验失败（数据无效/Inaccessible/权限）不清理提示，等他人处理
- 举报提示只回复被举报消息，正文不复制原文（对齐 antispam review 提示）
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from src.bot.handlers import moderation


@pytest.mark.unit
@pytest.mark.parametrize(
    ("handler", "callback_data", "process_name", "denial_key"),
    [
        (
            moderation.on_report_approve,
            "report_approve:123",
            "_process_report_approval",
            "moderation.report.callback.admin_only.approve.toast",
        ),
        (
            moderation.on_report_reject,
            "report_reject:123",
            "_process_report_rejection",
            "moderation.report.callback.admin_only.reject.toast",
        ),
        (
            moderation.on_report_ignore,
            "report_ignore:123",
            "_process_report_ignore",
            "moderation.report.callback.admin_only.ignore.toast",
        ),
    ],
)
async def test_report_callback_rejects_non_admin_clicker(
    handler,
    callback_data,
    process_name,
    denial_key,
) -> None:
    """权限检查必须使用点击者 ID；拒绝后不得执行处理，也不得清理提示。"""
    chat_id = -1001234567890
    clicker_id = 100200300
    bot_user_id = 999000999  # callback.message 的发送者（Bot 自身）

    callback = MagicMock(spec=CallbackQuery)
    callback.data = callback_data
    callback.answer = AsyncMock()
    callback.from_user = MagicMock()
    callback.from_user.id = clicker_id

    message = MagicMock(spec=Message)
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.from_user = MagicMock()
    message.from_user.id = bot_user_id
    message.edit_text = AsyncMock()
    callback.message = message

    bot = AsyncMock()
    permission_check = AsyncMock(return_value=False)
    process = AsyncMock()
    auto_delete = AsyncMock()
    localizer = MagicMock()
    localizer.t = MagicMock(side_effect=lambda key, **kw: f"<{key}>")

    with (
        patch.object(moderation, "check_admin_permission_strict", new=permission_check),
        patch.object(moderation, process_name, new=process),
        patch.object(moderation, "auto_delete_message", new=auto_delete),
    ):
        await handler(callback, bot, localizer)

    # 权限检查用的是点击者 ID，而非 Bot 的 ID
    permission_check.assert_awaited_once_with(bot, chat_id, clicker_id)
    callback.answer.assert_awaited_once_with(f"<{denial_key}>", show_alert=True)
    process.assert_not_awaited()
    message.edit_text.assert_not_awaited()
    auto_delete.assert_not_awaited()


def _localizer() -> MagicMock:
    """catalog mock：带变量返回 <key:{vars}>，否则返回 <key>。"""
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **variables: (
        f"<{key}:{variables}>" if variables else f"<{key}>"
    )
    return localizer


def _callback(callback_data: str) -> tuple[MagicMock, MagicMock]:
    """构造通过前置校验的最小 callback + message fixture。"""
    message = MagicMock(spec=Message)
    message.chat = SimpleNamespace(id=-1001234567890)
    message.edit_text = AsyncMock()

    callback = MagicMock(spec=CallbackQuery)
    callback.data = callback_data
    callback.message = message
    callback.answer = AsyncMock()
    callback.from_user = SimpleNamespace(
        id=100200300,
        full_name="Admin",
        first_name="Admin",
        username=None,
    )
    return callback, message


@pytest.mark.unit
@pytest.mark.parametrize(
    ("handler", "callback_data", "process_name", "success_key"),
    [
        (
            moderation.on_report_approve,
            "report_approve:123",
            "_process_report_approval",
            "moderation.report.callback.approved.message",
        ),
        (
            moderation.on_report_reject,
            "report_reject:123",
            "_process_report_rejection",
            "moderation.report.callback.rejected.message",
        ),
        (
            moderation.on_report_ignore,
            "report_ignore:123",
            "_process_report_ignore",
            "moderation.report.callback.ignored.message",
        ),
    ],
)
@pytest.mark.parametrize(
    ("outcome", "expected_key"),
    [
        ("success", None),
        ("failure", "moderation.report.callback.action_failed.message"),
        ("exception", "moderation.report.callback.action_failed.message"),
    ],
)
async def test_report_callback_cleans_prompt_after_processing(
    handler,
    callback_data,
    process_name,
    success_key,
    outcome,
    expected_key,
) -> None:
    """成功/业务失败/异常三类结果都必须移除按钮 + 安排 30s 删除。"""
    callback, message = _callback(callback_data)
    localizer = _localizer()
    bot = AsyncMock()

    if outcome == "success":
        process = AsyncMock(return_value=(True, ""))
    elif outcome == "failure":
        process = AsyncMock(return_value=(False, "business error"))
    else:
        process = AsyncMock(side_effect=RuntimeError("processor failed"))

    permission_check = AsyncMock(return_value=True)
    get_report = AsyncMock(
        return_value=SimpleNamespace(
            reason="system:spam",
            reported_user_id=42,
        )
    )
    auto_delete = AsyncMock()

    with (
        patch.object(moderation, "check_admin_permission_strict", new=permission_check),
        patch.object(moderation, process_name, new=process),
        patch.object(moderation.ReportRepository, "get_report_by_id", new=get_report),
        patch.object(moderation, "format_trusted_user_mention", return_value="Admin (ID:7)"),
        patch.object(moderation, "auto_delete_message", new=auto_delete),
    ):
        await handler(callback, bot, localizer)

    # processing toast 只 answer 一次（轻量不弹框）
    callback.answer.assert_awaited_once_with(
        "<moderation.report.callback.processing.toast>",
        show_alert=False,
    )
    process.assert_awaited_once()
    # edit_text 移除按钮，文案含对应结果 key
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.kwargs["reply_markup"] is None
    # 编辑同样关闭网页预览（否则举报原因中的链接会在此刻渲染出卡片）
    assert message.edit_text.await_args.kwargs["disable_web_page_preview"] is True
    rendered = message.edit_text.await_args.args[0]
    assert (success_key if outcome == "success" else expected_key) in rendered
    auto_delete.assert_awaited_once_with(message, delay=30)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("handler", "callback_data", "process_name"),
    [
        (
            moderation.on_report_approve,
            "report_approve:123",
            "_process_report_approval",
        ),
        (
            moderation.on_report_reject,
            "report_reject:123",
            "_process_report_rejection",
        ),
        (
            moderation.on_report_ignore,
            "report_ignore:123",
            "_process_report_ignore",
        ),
    ],
)
@pytest.mark.parametrize(
    ("preflight", "toast_key"),
    [
        ("invalid", "moderation.report.callback.invalid_data.toast"),
        ("inaccessible", "moderation.report.callback.inaccessible.toast"),
    ],
)
async def test_report_callback_preflight_failure_does_not_clean_prompt(
    handler,
    callback_data,
    process_name,
    preflight,
    toast_key,
) -> None:
    """数据无效/消息不可访问时不清理提示（提示仍有效，等他人处理）。"""
    callback, accessible_message = _callback(callback_data)
    localizer = _localizer()

    if preflight == "invalid":
        callback.data = None
        message = accessible_message
    else:
        message = MagicMock(spec=InaccessibleMessage)
        message.edit_text = AsyncMock()
        callback.message = message

    process = AsyncMock()
    permission_check = AsyncMock()
    auto_delete = AsyncMock()

    with (
        patch.object(moderation, "check_admin_permission_strict", new=permission_check),
        patch.object(moderation, process_name, new=process),
        patch.object(moderation, "auto_delete_message", new=auto_delete),
    ):
        await handler(callback, AsyncMock(), localizer)

    callback.answer.assert_awaited_once_with(f"<{toast_key}>", show_alert=True)
    permission_check.assert_not_awaited()
    process.assert_not_awaited()
    message.edit_text.assert_not_awaited()
    auto_delete.assert_not_awaited()


@pytest.mark.unit
async def test_process_report_ignore_marks_report_ignored() -> None:
    """忽略举报仅写 status=ignored，不触发封禁/训练。"""
    report = SimpleNamespace(
        group_id=-1001234567890,
        status="pending",
    )
    get_report = AsyncMock(return_value=report)
    update_status = AsyncMock(return_value=True)

    with (
        patch.object(moderation.ReportRepository, "get_report_by_id", new=get_report),
        patch.object(moderation.ReportRepository, "update_report_status", new=update_status),
    ):
        success, error = await moderation._process_report_ignore(
            report_id=123,
            chat_id=-1001234567890,
            operator_id=100200300,
            localizer=_localizer(),
        )

    assert success is True
    assert error == ""
    update_status.assert_awaited_once_with(
        report_id=123,
        status="ignored",
        handled_by=100200300,
    )


@pytest.mark.unit
def test_report_status_label_supports_ignored() -> None:
    """_report_status_label 识别 ignored 状态。"""
    localizer = _localizer()

    assert moderation._report_status_label(localizer, "ignored") == (
        "<moderation.report.status.ignored.label>"
    )
    localizer.t.assert_called_once_with("moderation.report.status.ignored.label")


@pytest.mark.unit
async def test_cmd_spam_prompt_replies_original_without_copying_content() -> None:
    """举报提示回复被举报消息、正文不复制原文；三按钮 + review TTL 不变。

    被举报原文仍完整落库（message_text 供 /reports 与 approve 后入训练库），
    只是不再复制进群内提示。
    """
    message = MagicMock(spec=Message)
    message.chat = SimpleNamespace(id=-1001234567890, type="supergroup")
    message.from_user = SimpleNamespace(id=100200300)
    message.text = "/spam"
    message.reply_to_message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        message_id=77,
        text="spam text",
        caption=None,
        content_type="text",
    )

    reply = MagicMock(spec=Message)
    message.answer = AsyncMock(return_value=reply)
    localizer = _localizer()
    report = SimpleNamespace(id=123)
    auto_delete = AsyncMock()
    create_report = AsyncMock(return_value=report)

    with (
        patch.object(
            moderation,
            "check_admin_permission_strict_message",
            new=AsyncMock(return_value=False),
        ),
        patch.object(moderation, "parse_spam_args", return_value=(False, None)),
        patch.object(
            moderation.ReportRepository,
            "count_user_reports",
            new=AsyncMock(return_value=0),
        ),
        patch.object(moderation.ReportRepository, "create_report", new=create_report),
        patch.object(
            moderation.ReportRepository,
            "count_pending_reports",
            new=AsyncMock(return_value=1),
        ),
        patch.object(
            moderation,
            "get_chat_administrators_mention",
            new=AsyncMock(return_value=""),
        ),
        patch.object(moderation, "auto_delete_message", new=auto_delete),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    # 原文照旧完整落库，不受提示不展示影响
    create_report.assert_awaited_once_with(
        group_id=-1001234567890,
        reporter_id=100200300,
        reported_user_id=42,
        message_id=77,
        message_text="spam text",
        reason=moderation._REPORT_DEFAULT_REASON_CODE,
    )

    answer_call = message.answer.await_args
    # 提示回复被举报消息；原消息已删则降级发送
    reply_parameters = answer_call.kwargs["reply_parameters"]
    assert reply_parameters.message_id == 77
    assert reply_parameters.allow_sending_without_reply is True
    # 举报原因为自由文本可能含链接，关闭网页预览
    assert answer_call.kwargs["disable_web_page_preview"] is True
    # 正文不复制被举报内容（localizer mock 会把变量原样渲进文本，可捕获残留实参）
    assert "spam text" not in answer_call.args[0]
    localizer.t.assert_any_call(
        "moderation.spam.report.submitted.message",
        report_id=123,
        reason="<moderation.spam.reason.default.label>",
        pending_count=1,
    )

    keyboard = answer_call.kwargs["reply_markup"]
    assert [len(row) for row in keyboard.inline_keyboard] == [2, 1]
    assert keyboard.inline_keyboard[0][0].callback_data == "report_approve:123"
    assert keyboard.inline_keyboard[0][1].callback_data == "report_reject:123"
    assert keyboard.inline_keyboard[1][0].callback_data == "report_ignore:123"
    auto_delete.assert_awaited_once_with(
        reply,
        delay=moderation.settings.spam_review_prompt_auto_delete_seconds,
    )
