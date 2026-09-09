"""集体投票 handler 测试（callback 资格校验 / 投票流转 / 终态处置 / 指令路径）。

范式对齐 ``test_spam_review_handler.py``：mock i18n 返回 key 本身，服务层
依赖全部 mocker.patch.object 隔离；断言「走了正确的 i18n key 与服务调用」
而非文案内容。
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import spam_vote as handler
from src.services.moderation import ModerationErrorCode, ModerationResult
from src.services.spam_vote import SpamVoteSession, SpamVoteSource, VoteOutcome

pytestmark = pytest.mark.unit

CHAT_ID = -100123
ORIG_MSG_ID = 321
VOTE_ID = "0123456789abcdef"
OFFENDER_ID = 42
VOTER_ID = 7
ADMIN_ID = 999


def _session(threshold: int = 5) -> SpamVoteSession:
    return SpamVoteSession(
        vote_id=VOTE_ID,
        source=SpamVoteSource.review,
        offender_user_id=OFFENDER_ID,
        report_id=None,
        threshold=threshold,
        sample_text="spam text",
    )


@pytest.fixture
def localizer(mocker):
    """mock i18n：t 返回 key 本身，resolver/translator 与 _answer_toast 解耦。"""
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **variables: key
    resolver = MagicMock()
    resolver.for_user = AsyncMock(return_value="zh-Hans")
    resolver.for_group = AsyncMock(return_value="zh-Hans")
    translator = MagicMock()
    translator.for_locale.return_value = localizer
    mocker.patch.object(handler, "get_resolver", return_value=resolver)
    mocker.patch.object(handler, "get_translator", return_value=translator)
    return localizer


def _message() -> MagicMock:
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID)
    message.delete = AsyncMock()
    message.answer = AsyncMock()
    return message


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.message = _message()
    callback.from_user = SimpleNamespace(id=VOTER_ID)
    callback.answer = AsyncMock()
    callback.bot = MagicMock()
    return callback


def _patch_lock(mocker, acquired: bool = True) -> None:
    @asynccontextmanager
    async def fake_lock(chat_id: int, orig_msg_id: int):
        yield acquired

    mocker.patch.object(handler, "review_lock", new=fake_lock)


def _patch_group(mocker, enabled: bool = True) -> AsyncMock:
    group = SimpleNamespace(spam_vote_enabled=enabled)
    return mocker.patch.object(handler.GroupRepository, "get", new=AsyncMock(return_value=group))


def _non_admin(mocker) -> AsyncMock:
    mocker.patch.object(handler.settings, "admin_ids", [])
    return mocker.patch.object(
        handler.PermissionCache, "is_admin", new=AsyncMock(return_value=False)
    )


async def test_callback_invalid_format_answers_invalid_data(mocker, localizer) -> None:
    callback = _callback("spam_vote:up:not-an-id:bad")

    await handler.on_spam_vote_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with(
        "antispam.callback.invalid_data.toast", show_alert=True
    )


async def test_callback_disabled_answers_disabled(mocker, localizer) -> None:
    callback = _callback(f"spam_vote:up:{ORIG_MSG_ID}:{VOTE_ID}")
    _patch_group(mocker, enabled=False)
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=_session()))

    await handler.on_spam_vote_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with("spam_vote.callback.disabled.toast", show_alert=True)


async def test_callback_admin_redirected_to_direct_buttons(mocker, localizer) -> None:
    callback = _callback(f"spam_vote:up:{ORIG_MSG_ID}:{VOTE_ID}")
    callback.from_user = SimpleNamespace(id=ADMIN_ID)
    _patch_group(mocker)
    mocker.patch.object(handler.settings, "admin_ids", [ADMIN_ID])
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=_session()))

    await handler.on_spam_vote_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with(
        "spam_vote.callback.admin_direct.toast", show_alert=True
    )


async def test_callback_offender_cannot_vote(mocker, localizer) -> None:
    callback = _callback(f"spam_vote:up:{ORIG_MSG_ID}:{VOTE_ID}")
    callback.from_user = SimpleNamespace(id=OFFENDER_ID)
    _patch_group(mocker)
    _non_admin(mocker)
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=_session()))

    await handler.on_spam_vote_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with("spam_vote.callback.offender.toast", show_alert=True)


async def test_callback_expired_session_deletes_prompt(mocker, localizer) -> None:
    callback = _callback(f"spam_vote:up:{ORIG_MSG_ID}:{VOTE_ID}")
    _patch_group(mocker)
    _non_admin(mocker)
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=None))

    await handler.on_spam_vote_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with("spam_vote.callback.expired.toast", show_alert=True)
    callback.message.delete.assert_awaited_once()


async def test_callback_already_voted(mocker, localizer) -> None:
    callback = _callback(f"spam_vote:up:{ORIG_MSG_ID}:{VOTE_ID}")
    _patch_group(mocker)
    _non_admin(mocker)
    session = _session()
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=session))
    cast = mocker.patch.object(
        handler,
        "cast_vote",
        new=AsyncMock(return_value=VoteOutcome(status="already", up=2, down=1)),
    )

    await handler.on_spam_vote_callback(callback, MagicMock())

    cast.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, VOTER_ID, "up", expected_vote_id=VOTE_ID)
    callback.answer.assert_awaited_once_with(
        "spam_vote.callback.already_voted.toast", show_alert=True
    )


async def test_callback_vote_updates_progress_without_finalize(mocker, localizer) -> None:
    """未达阈值：进度 toast + 提示进度编辑，不触发终局。"""
    callback = _callback(f"spam_vote:down:{ORIG_MSG_ID}:{VOTE_ID}")
    _patch_group(mocker)
    _non_admin(mocker)
    session = _session()
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=session))
    mocker.patch.object(
        handler,
        "cast_vote",
        new=AsyncMock(return_value=VoteOutcome(status="voted", up=1, down=3)),
    )
    finalize = mocker.patch.object(
        handler, "finalize_vote_if_ready", new=AsyncMock(return_value=False)
    )
    edit_progress = mocker.patch.object(handler, "edit_vote_progress", new=AsyncMock())
    bot = MagicMock()

    await handler.on_spam_vote_callback(callback, bot)

    callback.answer.assert_awaited_once_with("spam_vote.callback.voted.toast", show_alert=False)
    finalize.assert_awaited_once()
    edit_progress.assert_awaited_once()
    assert edit_progress.await_args.args == (bot, CHAT_ID, ORIG_MSG_ID, 1, 3, 5)


async def test_callback_threshold_triggers_ban(mocker, localizer) -> None:
    """+阈值：锁内消费会话 + review state、ban（allow_left）、审计、批量置 approved、
    删原消息、编辑提示结果并 30s 自删。"""
    callback = _callback(f"spam_vote:up:{ORIG_MSG_ID}:{VOTE_ID}")
    _patch_group(mocker)
    _non_admin(mocker)
    _patch_lock(mocker)
    session = _session()
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=session))
    mocker.patch.object(
        handler,
        "cast_vote",
        new=AsyncMock(return_value=VoteOutcome(status="voted", up=5, down=0)),
    )
    consume_vote = mocker.patch.object(
        handler, "consume_vote_session", new=AsyncMock(return_value=session)
    )
    review_state = SimpleNamespace(review_id="aaaabbbbccccdddd")
    mocker.patch.object(handler, "get_review_state", new=AsyncMock(return_value=review_state))
    consume_review = mocker.patch.object(handler, "consume_review_state", new=AsyncMock())
    ban = mocker.patch.object(
        handler.ModerationService,
        "ban_user",
        new=AsyncMock(return_value=ModerationResult(code=None)),
    )
    detector = MagicMock()
    detector.add_feedback = AsyncMock()
    mocker.patch.object(handler, "get_detector", return_value=detector)
    audit = mocker.patch.object(handler.AuditRepository, "log_action", new=AsyncMock())
    update_reports = mocker.patch.object(
        handler.ReportRepository, "update_reports_status_by_message", new=AsyncMock()
    )
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    mocker.patch.object(handler, "get_vote_prompt", new=AsyncMock(return_value=(555, "base")))
    auto_delete = mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())

    await handler.on_spam_vote_callback(callback, bot)

    consume_vote.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, session.vote_id)
    consume_review.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, review_state.review_id)
    ban.assert_awaited_once_with(
        bot=bot,
        chat_id=CHAT_ID,
        user_id=OFFENDER_ID,
        operator_id=VOTER_ID,
        reason="垃圾信息（集体投票确认）",
        revoke_messages=False,
        allow_left=True,
    )
    detector.add_feedback.assert_awaited_once_with(
        text="spam text", is_spam=True, labeled_by=VOTER_ID
    )
    assert audit.await_args.kwargs["action"] == "spam_vote_ban"
    update_reports.assert_awaited_once_with(
        group_id=CHAT_ID, message_id=ORIG_MSG_ID, status="approved", handled_by=VOTER_ID
    )
    bot.delete_message.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID)
    # 结果段编辑进提示消息（移除按钮）+ 30s 自删
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    auto_delete.assert_awaited_once_with(callback.message, delay=30)


async def test_callback_ham_threshold_keeps_message(mocker, localizer) -> None:
    """-阈值：入负样本、审计、批量置 rejected、不 ban 不删原消息。"""
    callback = _callback(f"spam_vote:down:{ORIG_MSG_ID}:{VOTE_ID}")
    _patch_group(mocker)
    _non_admin(mocker)
    _patch_lock(mocker)
    session = _session()
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=session))
    mocker.patch.object(
        handler,
        "cast_vote",
        new=AsyncMock(return_value=VoteOutcome(status="voted", up=0, down=5)),
    )
    mocker.patch.object(handler, "consume_vote_session", new=AsyncMock(return_value=session))
    mocker.patch.object(handler, "get_review_state", new=AsyncMock(return_value=None))
    ban = mocker.patch.object(handler.ModerationService, "ban_user", new=AsyncMock())
    detector = MagicMock()
    detector.add_feedback = AsyncMock()
    mocker.patch.object(handler, "get_detector", return_value=detector)
    audit = mocker.patch.object(handler.AuditRepository, "log_action", new=AsyncMock())
    update_reports = mocker.patch.object(
        handler.ReportRepository, "update_reports_status_by_message", new=AsyncMock()
    )
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    mocker.patch.object(handler, "get_vote_prompt", new=AsyncMock(return_value=None))

    await handler.on_spam_vote_callback(callback, bot)

    ban.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    detector.add_feedback.assert_awaited_once_with(
        text="spam text", is_spam=False, labeled_by=VOTER_ID
    )
    assert audit.await_args.kwargs["action"] == "spam_vote_false_positive"
    update_reports.assert_awaited_once_with(
        group_id=CHAT_ID, message_id=ORIG_MSG_ID, status="rejected", handled_by=VOTER_ID
    )


async def test_callback_ban_failure_shows_action_failed(mocker, localizer) -> None:
    """ban 失败：审计 spam_vote_ban_failed + 结果段为 action_failed 文案。"""
    callback = _callback(f"spam_vote:up:{ORIG_MSG_ID}:{VOTE_ID}")
    _patch_group(mocker)
    _non_admin(mocker)
    _patch_lock(mocker)
    session = _session()
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=session))
    mocker.patch.object(
        handler,
        "cast_vote",
        new=AsyncMock(return_value=VoteOutcome(status="voted", up=5, down=0)),
    )
    mocker.patch.object(handler, "consume_vote_session", new=AsyncMock(return_value=session))
    mocker.patch.object(handler, "get_review_state", new=AsyncMock(return_value=None))
    mocker.patch.object(
        handler.ModerationService,
        "ban_user",
        new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.operation_failed)),
    )
    audit = mocker.patch.object(handler.AuditRepository, "log_action", new=AsyncMock())
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    mocker.patch.object(handler, "get_vote_prompt", new=AsyncMock(return_value=(555, "base")))

    await handler.on_spam_vote_callback(callback, bot)

    assert audit.await_args.kwargs["action"] == "spam_vote_ban_failed"
    text = bot.edit_message_text.await_args.kwargs["text"]
    assert "spam_vote.action_failed.message" in text


async def test_handle_vote_command_no_session(mocker, localizer) -> None:
    message = _message()
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=None))
    auto_delete = mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())

    await handler.handle_vote_command(MagicMock(), message, localizer, ORIG_MSG_ID, "down")

    message.answer.assert_awaited_once_with("spam_vote.command.no_session.message")
    auto_delete.assert_awaited_once()


async def test_handle_vote_command_voted_replies_progress(mocker, localizer) -> None:
    message = _message()
    message.from_user = SimpleNamespace(id=VOTER_ID)
    _patch_group(mocker)
    _non_admin(mocker)
    session = _session()
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=session))
    mocker.patch.object(
        handler,
        "cast_vote",
        new=AsyncMock(return_value=VoteOutcome(status="voted", up=0, down=2)),
    )
    finalize = mocker.patch.object(
        handler, "finalize_vote_if_ready", new=AsyncMock(return_value=False)
    )
    mocker.patch.object(handler, "edit_vote_progress", new=AsyncMock())
    auto_delete = mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())

    await handler.handle_vote_command(MagicMock(), message, localizer, ORIG_MSG_ID, "down")

    message.answer.assert_awaited_once_with("spam_vote.command.voted.message")
    auto_delete.assert_awaited_once()
    finalize.assert_awaited_once()


async def test_handle_vote_command_admin_redirected(mocker, localizer) -> None:
    message = _message()
    message.from_user = SimpleNamespace(id=ADMIN_ID)
    mocker.patch.object(handler.settings, "admin_ids", [ADMIN_ID])
    _patch_group(mocker)
    mocker.patch.object(handler, "get_vote_session", new=AsyncMock(return_value=_session()))
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())

    await handler.handle_vote_command(MagicMock(), message, localizer, ORIG_MSG_ID, "up")

    message.answer.assert_awaited_once_with("spam_vote.command.admin_direct.message")


async def test_finalize_aborts_when_consumed_session_rebuilt(mocker, localizer) -> None:
    """消费身份不匹配（窗口边缘过期后被重建为新会话）放弃处置——防旧参数终局。

    回归契约（codex review High）：consume 不匹配时不删除并返回当前新会话，
    finalize 必须校验 vote_id 一致才继续，否则新会话残留可再次触发终局（双罚）。
    """
    _patch_lock(mocker)
    _patch_group(mocker)
    session = _session()
    rebuilt = _session()  # vote_id 相同的工厂默认值——显式改为不同 ID
    object.__setattr__(rebuilt, "vote_id", "fedcba9876543210")
    mocker.patch.object(handler, "consume_vote_session", new=AsyncMock(return_value=rebuilt))
    mocker.patch.object(handler, "get_vote_prompt", new=AsyncMock(return_value=None))
    ban = mocker.patch.object(handler.ModerationService, "ban_user", new=AsyncMock())
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    finalized = await handler.finalize_vote_if_ready(
        bot, CHAT_ID, ORIG_MSG_ID, session, VOTER_ID, "up", 5, 0
    )

    assert finalized is True
    ban.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()


async def test_finalize_aborts_when_group_disabled_in_lock(mocker, localizer) -> None:
    """锁内复查开关：资格校验后管理员关闭投票，达阈请求放弃终局（会话冻结不消费）。

    回归契约（codex review Medium）：开关检查与 cast_vote 存在毫秒级窗口，
    关闭后的在途达阈请求不得执行处罚。
    """
    _patch_lock(mocker)
    session = _session()
    # 直接调 finalize 模拟"资格校验已过、锁内复查时开关已关"的窗口翻转
    group_off = SimpleNamespace(spam_vote_enabled=False)
    mocker.patch.object(handler.GroupRepository, "get", new=AsyncMock(return_value=group_off))
    mocker.patch.object(handler, "get_vote_prompt", new=AsyncMock(return_value=None))
    consume = mocker.patch.object(
        handler, "consume_vote_session", new=AsyncMock(return_value=session)
    )
    ban = mocker.patch.object(handler.ModerationService, "ban_user", new=AsyncMock())
    bot = MagicMock()

    finalized = await handler.finalize_vote_if_ready(
        bot, CHAT_ID, ORIG_MSG_ID, session, VOTER_ID, "up", 5, 0
    )

    assert finalized is True
    # 会话未被消费（保持冻结态）、处罚未执行
    consume.assert_not_awaited()
    ban.assert_not_awaited()
