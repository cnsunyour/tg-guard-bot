"""反垃圾 review producer/consumer + tombstone + ban_user allow_left 测试。

覆盖 3b-3 核心契约：
- producer：NX 写 state、已存在不发、发送失败 CAS 清理
- consumer：格式错 / 无权限 / state 过期 / review_id 不匹配（P2）/ ban 成功（allow_left）/ ban 失败（toast 不破坏消息）/ false_positive（保留原消息）/ 锁未取得
- tombstone：旧 spam_confirm 只 legacy.toast + 删提示，不处罚
- ban_user allow_left：left 用户可封（review 路径），默认仍拒绝
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiogram.types import ReplyParameters

from src.bot.handlers import antispam
from src.services import moderation
from src.services.moderation import ModerationErrorCode, ModerationResult
from src.services.spam_review import SpamMessageType, SpamReviewState

pytestmark = pytest.mark.unit

REVIEW_ID = "0123456789abcdef"
OTHER_REVIEW_ID = "fedcba9876543210"
CHAT_ID = -100123
ORIG_MSG_ID = 321
OFFENDER_ID = 42
OPERATOR_ID = 7


@pytest.fixture
def localizer(mocker):
    """mock i18n：t 返回 key 本身（含 error 时附 error 值），便于断言。"""
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **variables: (
        f"{key}:{variables['error']}" if "error" in variables else key
    )
    resolver = MagicMock()
    resolver.for_user = AsyncMock(return_value="zh-Hans")
    resolver.for_group = AsyncMock(return_value="zh-Hans")
    translator = MagicMock()
    translator.for_locale.return_value = localizer
    mocker.patch.object(antispam, "get_resolver", return_value=resolver)
    mocker.patch.object(antispam, "get_translator", return_value=translator)
    return localizer


def _message(*, answer_side_effect=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=CHAT_ID),
        from_user=SimpleNamespace(
            id=OFFENDER_ID,
            full_name="Offender",
            first_name="Offender",
            username=None,
        ),
        message_id=ORIG_MSG_ID,
        text="spam text",
        caption=None,
        reply_markup="review-keyboard",
        answer=AsyncMock(side_effect=answer_side_effect),
        delete=AsyncMock(),
        edit_text=AsyncMock(),
    )


def _callback(data: str, message):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=OPERATOR_ID,
            full_name="Admin",
            first_name="Admin",
            username=None,
        ),
        message=message,
        answer=AsyncMock(),
    )


def _state(review_id: str = REVIEW_ID) -> SpamReviewState:
    return SpamReviewState(
        review_id=review_id,
        offender_user_id=OFFENDER_ID,
        message_type=SpamMessageType.text,
        original_text="original text",
        recognized_text=None,
        sample_text="sample text",
        reason_codes=("rule:url",),
        confidence=0.91,
    )


def _patch_lock(mocker, acquired: bool) -> None:
    @asynccontextmanager
    async def fake_review_lock(chat_id: int, orig_msg_id: int):
        yield acquired

    mocker.patch.object(antispam, "review_lock", new=fake_review_lock)


def _authorize(mocker, allowed: bool = True):
    mocker.patch.object(antispam.settings, "admin_ids", [])
    return mocker.patch.object(
        antispam.PermissionCache, "is_admin", new=AsyncMock(return_value=allowed)
    )


async def test_review_producer_creates_nx_state_and_sends_prompt(mocker, localizer) -> None:
    message = _message()
    create_state = mocker.patch.object(
        antispam,
        "create_review_state",
        new=AsyncMock(side_effect=lambda state, *args, **kwargs: state),
    )
    mocker.patch.object(antispam, "format_user_mention", return_value="Offender")
    mocker.patch.object(
        antispam, "get_chat_administrators_mention", new=AsyncMock(return_value="@admins")
    )
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())

    await antispam._handle_spam_with_review(
        message,
        MagicMock(),
        {"reasons": ["rule:url"], "confidence": 0.91},
        message_type=SpamMessageType.text,
    )

    state = create_state.await_args.args[0]
    assert state.offender_user_id == OFFENDER_ID
    assert state.original_text == "spam text"
    assert state.sample_text == "spam text"
    assert state.message_type is SpamMessageType.text
    review_ttl = antispam.settings.spam_review_prompt_auto_delete_seconds
    create_state.assert_awaited_once_with(state, CHAT_ID, ORIG_MSG_ID, ttl=review_ttl)
    message.answer.assert_awaited_once()
    # 提示回复被检测消息（原文由回复引用展示，不复制进正文）；原消息已删则降级发送
    reply_parameters = message.answer.await_args.kwargs["reply_parameters"]
    assert isinstance(reply_parameters, ReplyParameters)
    assert reply_parameters.message_id == ORIG_MSG_ID
    assert reply_parameters.allow_sending_without_reply is True
    # 检测原因可能含可疑域名，关闭网页预览
    assert message.answer.await_args.kwargs["disable_web_page_preview"] is True
    # prompt 发出后安排与 state TTL 一致的自动删除（兜底未处理残留）
    auto_delete.assert_awaited_once_with(message.answer.return_value, delay=review_ttl)


async def test_review_producer_does_not_send_when_state_already_exists(mocker, localizer) -> None:
    message = _message()
    mocker.patch.object(antispam, "create_review_state", new=AsyncMock(return_value=None))

    await antispam._handle_spam_with_review(
        message,
        MagicMock(),
        {"reasons": ["rule:url"], "confidence": 0.91},
        message_type=SpamMessageType.text,
    )

    message.answer.assert_not_awaited()


async def test_review_producer_cleans_state_when_prompt_send_fails(mocker, localizer) -> None:
    message = _message(answer_side_effect=RuntimeError("send failed"))
    create_state = mocker.patch.object(
        antispam,
        "create_review_state",
        new=AsyncMock(side_effect=lambda state, *args, **kwargs: state),
    )
    cleanup = mocker.patch.object(
        antispam, "delete_review_state_if_match", new=AsyncMock(return_value=True)
    )
    mocker.patch.object(antispam, "format_user_mention", return_value="Offender")
    mocker.patch.object(antispam, "get_chat_administrators_mention", new=AsyncMock(return_value=""))

    with pytest.raises(RuntimeError, match="send failed"):
        await antispam._handle_spam_with_review(
            message,
            MagicMock(),
            {"reasons": ["rule:url"], "confidence": 0.91},
            message_type=SpamMessageType.text,
        )

    state = create_state.await_args.args[0]
    cleanup.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, state.review_id)


async def test_review_callback_invalid_format_answers_invalid_data(mocker, localizer) -> None:
    callback = _callback("spam_review:ban:not-an-id:bad", _message())
    permission = _authorize(mocker)

    await antispam.on_spam_review_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with(
        "antispam.callback.invalid_data.toast", show_alert=True
    )
    permission.assert_not_awaited()


async def test_review_callback_rejects_non_admin(mocker, localizer) -> None:
    callback = _callback(f"spam_review:ban:{ORIG_MSG_ID}:{REVIEW_ID}", _message())
    permission = _authorize(mocker, allowed=False)

    await antispam.on_spam_review_callback(callback, MagicMock())

    permission.assert_awaited_once()
    callback.answer.assert_awaited_once_with(
        "antispam.callback.permission_denied.toast", show_alert=True
    )


async def test_review_callback_missing_state_expires_and_deletes_prompt(mocker, localizer) -> None:
    message = _message()
    callback = _callback(f"spam_review:ban:{ORIG_MSG_ID}:{REVIEW_ID}", message)
    _authorize(mocker)
    _patch_lock(mocker, acquired=True)
    mocker.patch.object(antispam, "get_review_state", new=AsyncMock(return_value=None))

    await antispam.on_spam_review_callback(callback, MagicMock())

    # processing（不弹框）；expired 不再 callback.answer（Telegram 仅允许一次），只删提示
    assert callback.answer.await_args_list == [
        call("antispam.callback.processing.toast", show_alert=False),
    ]
    message.delete.assert_awaited_once()


async def test_review_callback_mismatched_review_id_expires_and_deletes_prompt(
    mocker, localizer
) -> None:
    """P2：旧 prompt 按钮消费被重建的新 state → expired（不误封）。"""
    message = _message()
    callback = _callback(f"spam_review:ban:{ORIG_MSG_ID}:{REVIEW_ID}", message)
    _authorize(mocker)
    _patch_lock(mocker, acquired=True)
    mocker.patch.object(
        antispam, "get_review_state", new=AsyncMock(return_value=_state(OTHER_REVIEW_ID))
    )

    await antispam.on_spam_review_callback(callback, MagicMock())

    # P2：不二次 answer（已 answer processing），只删旧提示
    assert callback.answer.await_args_list == [
        call("antispam.callback.processing.toast", show_alert=False),
    ]
    message.delete.assert_awaited_once()


async def test_review_callback_ban_success_consumes_state_and_allows_left(
    mocker, localizer
) -> None:
    message = _message()
    callback = _callback(f"spam_review:ban:{ORIG_MSG_ID}:{REVIEW_ID}", message)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    _authorize(mocker)
    _patch_lock(mocker, acquired=True)
    mocker.patch.object(antispam, "get_review_state", new=AsyncMock(return_value=_state()))
    ban = mocker.patch.object(
        antispam.ModerationService, "ban_user", new=AsyncMock(return_value=ModerationResult())
    )
    detector = SimpleNamespace(add_feedback=AsyncMock())
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    audit = mocker.patch.object(antispam.AuditRepository, "log_action", new=AsyncMock())
    consume = mocker.patch.object(antispam, "consume_review_state", new=AsyncMock())
    mocker.patch.object(antispam, "format_trusted_user_mention", return_value="Admin")
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())

    await antispam.on_spam_review_callback(callback, bot)

    ban.assert_awaited_once_with(
        bot=bot,
        chat_id=CHAT_ID,
        user_id=OFFENDER_ID,
        operator_id=OPERATOR_ID,
        reason="垃圾信息（管理员确认）",
        revoke_messages=False,
        allow_left=True,
    )
    detector.add_feedback.assert_awaited_once_with(
        text="sample text", is_spam=True, labeled_by=OPERATOR_ID
    )
    audit.assert_awaited_once_with(
        group_id=CHAT_ID,
        operator_id=OPERATOR_ID,
        action="spam_review_ban",
        target_user_id=OFFENDER_ID,
        details={"orig_msg_id": ORIG_MSG_ID, "text_preview": "sample text"},
    )
    bot.delete_message.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID)
    consume.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, REVIEW_ID)
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.kwargs["reply_markup"] is None
    # 编辑同样关闭网页预览（否则原因中的可疑域名会在此刻渲染出卡片）
    assert message.edit_text.await_args.kwargs["disable_web_page_preview"] is True
    auto_delete.assert_awaited_once_with(message, delay=30)


async def test_review_callback_ban_failure_consumes_state_and_cleans_prompt(
    mocker, localizer
) -> None:
    """处罚失败：显示失败原因、消费 state、审计 spam_review_ban_failed 并清理 prompt。

    不再保留按钮重试（原失败分支 return 不清理之弊）；重试需重新触发检测。
    """
    message = _message()
    callback = _callback(f"spam_review:ban:{ORIG_MSG_ID}:{REVIEW_ID}", message)
    _authorize(mocker)
    _patch_lock(mocker, acquired=True)
    mocker.patch.object(antispam, "get_review_state", new=AsyncMock(return_value=_state()))
    mocker.patch.object(
        antispam.ModerationService,
        "ban_user",
        new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.operation_failed)),
    )
    audit = mocker.patch.object(antispam.AuditRepository, "log_action", new=AsyncMock())
    consume = mocker.patch.object(antispam, "consume_review_state", new=AsyncMock())
    mocker.patch.object(antispam, "format_trusted_user_mention", return_value="Admin")
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())

    await antispam.on_spam_review_callback(callback, MagicMock())

    # 失败仍消费 state + 审计 ban_failed + 清理 prompt（杜绝残留）
    consume.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, REVIEW_ID)
    audit.assert_awaited_once_with(
        group_id=CHAT_ID,
        operator_id=OPERATOR_ID,
        action="spam_review_ban_failed",
        target_user_id=OFFENDER_ID,
        details={
            "orig_msg_id": ORIG_MSG_ID,
            "text_preview": "sample text",
            "error_code": "operation_failed",
        },
    )
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.kwargs["reply_markup"] is None
    edit_text = message.edit_text.await_args.args[0]
    # ban 失败 code 透传为 moderation.error.<code>.message，再注入 review action_failed
    assert "moderation.error.operation_failed.message" in edit_text
    assert "antispam.review.action_failed.message" in edit_text
    assert "spam text" in edit_text  # 原 message.text 保留
    auto_delete.assert_awaited_once_with(message, delay=30)


async def test_review_callback_false_positive_keeps_original_message(mocker, localizer) -> None:
    message = _message()
    callback = _callback(f"spam_review:false_positive:{ORIG_MSG_ID}:{REVIEW_ID}", message)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    _authorize(mocker)
    _patch_lock(mocker, acquired=True)
    mocker.patch.object(antispam, "get_review_state", new=AsyncMock(return_value=_state()))
    detector = SimpleNamespace(add_feedback=AsyncMock())
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    mocker.patch.object(antispam.AuditRepository, "log_action", new=AsyncMock())
    consume = mocker.patch.object(antispam, "consume_review_state", new=AsyncMock())
    mocker.patch.object(antispam, "format_trusted_user_mention", return_value="Admin")
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())

    await antispam.on_spam_review_callback(callback, bot)

    detector.add_feedback.assert_awaited_once_with(
        text="sample text", is_spam=False, labeled_by=OPERATOR_ID
    )
    consume.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, REVIEW_ID)
    bot.delete_message.assert_not_awaited()  # 保留原消息
    message.delete.assert_not_awaited()
    assert message.edit_text.await_args.kwargs["reply_markup"] is None
    auto_delete.assert_awaited_once_with(message, delay=30)


async def test_review_callback_ignore_closes_without_punishment(mocker, localizer) -> None:
    """忽略：不处罚、不入库、不删原消息，仅消费 state + 审计 spam_review_ignore + 清理 prompt。"""
    message = _message()
    callback = _callback(f"spam_review:ignore:{ORIG_MSG_ID}:{REVIEW_ID}", message)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    _authorize(mocker)
    _patch_lock(mocker, acquired=True)
    mocker.patch.object(antispam, "get_review_state", new=AsyncMock(return_value=_state()))
    ban = mocker.patch.object(antispam.ModerationService, "ban_user", new=AsyncMock())
    detector = SimpleNamespace(add_feedback=AsyncMock())
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    audit = mocker.patch.object(antispam.AuditRepository, "log_action", new=AsyncMock())
    consume = mocker.patch.object(antispam, "consume_review_state", new=AsyncMock())
    mocker.patch.object(antispam, "format_trusted_user_mention", return_value="Admin")
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())

    await antispam.on_spam_review_callback(callback, bot)

    ban.assert_not_awaited()
    detector.add_feedback.assert_not_awaited()
    bot.delete_message.assert_not_awaited()  # 保留原消息
    consume.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, REVIEW_ID)
    audit.assert_awaited_once_with(
        group_id=CHAT_ID,
        operator_id=OPERATOR_ID,
        action="spam_review_ignore",
        target_user_id=OFFENDER_ID,
        details={"orig_msg_id": ORIG_MSG_ID, "text_preview": "sample text"},
    )
    assert message.edit_text.await_args.kwargs["reply_markup"] is None
    auto_delete.assert_awaited_once_with(message, delay=30)


async def test_review_callback_ban_success_cleans_prompt_even_if_feedback_fails(
    mocker, localizer
) -> None:
    """try/finally 兜底：ban 成功但 add_feedback 抛异常时，finally 仍消费 state + 清理 prompt。"""
    message = _message()
    callback = _callback(f"spam_review:ban:{ORIG_MSG_ID}:{REVIEW_ID}", message)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    _authorize(mocker)
    _patch_lock(mocker, acquired=True)
    mocker.patch.object(antispam, "get_review_state", new=AsyncMock(return_value=_state()))
    mocker.patch.object(
        antispam.ModerationService, "ban_user", new=AsyncMock(return_value=ModerationResult())
    )
    detector = SimpleNamespace(add_feedback=AsyncMock(side_effect=RuntimeError("db down")))
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    mocker.patch.object(antispam.AuditRepository, "log_action", new=AsyncMock())
    consume = mocker.patch.object(antispam, "consume_review_state", new=AsyncMock())
    mocker.patch.object(antispam, "format_trusted_user_mention", return_value="Admin")
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())

    await antispam.on_spam_review_callback(callback, bot)

    # add_feedback 异常被独立 suppress；completed_text 已在 ban 成功时设置；
    # finally 仍消费 state + edit（ban 结果）+ auto_delete，杜绝残留
    consume.assert_awaited_once_with(CHAT_ID, ORIG_MSG_ID, REVIEW_ID)
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.kwargs["reply_markup"] is None
    auto_delete.assert_awaited_once_with(message, delay=30)


async def test_review_callback_returns_after_processing_when_lock_not_acquired(
    mocker, localizer
) -> None:
    callback = _callback(f"spam_review:ban:{ORIG_MSG_ID}:{REVIEW_ID}", _message())
    _authorize(mocker)
    _patch_lock(mocker, acquired=False)
    state = mocker.patch.object(antispam, "get_review_state", new=AsyncMock())

    await antispam.on_spam_review_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with("antispam.callback.processing.toast", show_alert=False)
    state.assert_not_awaited()


async def test_legacy_spam_confirm_callback_authorizes_then_expires_prompt(
    mocker, localizer
) -> None:
    message = _message()
    callback = _callback("spam_confirm:ban:42:1", message)
    _authorize(mocker)  # P2：tombstone 删提示前要管理员权限

    await antispam.on_spam_confirm_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with("antispam.review.legacy.toast", show_alert=True)
    message.delete.assert_awaited_once()


async def test_legacy_spam_confirm_callback_rejects_non_admin(mocker, localizer) -> None:
    callback = _callback("spam_confirm:ban:42:1", _message())
    _authorize(mocker, allowed=False)

    await antispam.on_spam_confirm_callback(callback, MagicMock())

    callback.answer.assert_awaited_once_with(
        "antispam.callback.permission_denied.toast", show_alert=True
    )


async def test_ban_user_allow_left_bans_a_left_member(mocker) -> None:
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="left"))
    bot.ban_chat_member = AsyncMock()
    mocker.patch.object(moderation.AuditRepository, "log_action", new=AsyncMock())

    result = await moderation.ModerationService.ban_user(
        bot, CHAT_ID, OFFENDER_ID, OPERATOR_ID, allow_left=True
    )

    assert result.success is True
    assert result.code is None
    bot.ban_chat_member.assert_awaited_once_with(
        chat_id=CHAT_ID, user_id=OFFENDER_ID, revoke_messages=False
    )


async def test_ban_user_default_rejects_a_left_member() -> None:
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="left"))
    bot.ban_chat_member = AsyncMock()

    result = await moderation.ModerationService.ban_user(bot, CHAT_ID, OFFENDER_ID, OPERATOR_ID)

    assert result.success is False
    assert result.code is ModerationErrorCode.user_not_in_chat
    bot.ban_chat_member.assert_not_awaited()


@pytest.mark.parametrize(
    (
        "confidence",
        "service_name",
        "other_service_name",
        "duration",
        "punishment_key",
        "expected_reason",
    ),
    [
        (
            0.95,
            "ban_user_temporarily",
            "mute_user",
            60,
            "temporary_ban",
            "垃圾信息（高置信度）: rule:url",
        ),
        (
            0.5,
            "mute_user",
            "ban_user_temporarily",
            10,
            "mute",
            "垃圾信息: rule:url",
        ),
    ],
)
async def test_apply_immediate_punishment_selects_action_and_records_feedback(
    mocker,
    localizer,
    confidence,
    service_name,
    other_service_name,
    duration,
    punishment_key,
    expected_reason,
) -> None:
    message = _message()
    bot = SimpleNamespace(id=999)
    result = {"confidence": confidence, "reasons": ["rule:url"]}
    mocker.patch.object(antispam.settings, "spam_high_confidence_threshold", 0.8)
    selected_service = mocker.patch.object(
        antispam.ModerationService, service_name, new=AsyncMock(return_value=ModerationResult())
    )
    other_service = mocker.patch.object(
        antispam.ModerationService, other_service_name, new=AsyncMock()
    )
    redis = SimpleNamespace(setex=AsyncMock())
    mocker.patch.object(antispam, "get_redis", return_value=redis)
    detector = SimpleNamespace(add_feedback=AsyncMock())
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    mocker.patch.object(
        antispam, "get_chat_administrators_mention", new=AsyncMock(return_value="@admins")
    )
    mocker.patch.object(antispam, "format_user_mention", return_value="Offender")
    render = mocker.patch.object(antispam, "build_immediate_processed", return_value="processed")
    keyboard = mocker.patch.object(antispam, "build_immediate_keyboard", return_value="keyboard")
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())

    await antispam._apply_immediate_punishment(
        message,
        bot,
        result,
        message_type=SpamMessageType.photo,
        recognized_text="recognized text",
    )

    message.delete.assert_awaited_once()
    selected_service.assert_awaited_once_with(
        bot=bot,
        chat_id=CHAT_ID,
        user_id=OFFENDER_ID,
        operator_id=999,
        duration=duration,
        reason=expected_reason,
    )
    other_service.assert_not_awaited()
    redis.setex.assert_awaited_once_with(
        antispam.RedisKeys.spam_message_text(CHAT_ID, ORIG_MSG_ID), 86400, "recognized text"
    )
    render.assert_called_once_with(
        localizer,
        message_type=SpamMessageType.photo,
        offender_mention="Offender",
        reason_codes=("rule:url",),
        confidence=confidence,
        punishment_key=punishment_key,
        message_id=ORIG_MSG_ID,
    )
    keyboard.assert_called_once_with(localizer, OFFENDER_ID, ORIG_MSG_ID)
    message.answer.assert_awaited_once_with("🔔 @admins\n\nprocessed", reply_markup="keyboard")
    auto_delete.assert_awaited_once_with(message.answer.return_value)
    detector.add_feedback.assert_awaited_once_with(
        text="recognized text", is_spam=True, labeled_by=999, confidence=confidence
    )


async def test_apply_immediate_punishment_stops_when_action_fails(mocker) -> None:
    message = _message()
    bot = SimpleNamespace(id=999)
    mocker.patch.object(antispam.settings, "spam_high_confidence_threshold", 0.8)
    ban = mocker.patch.object(
        antispam.ModerationService,
        "ban_user_temporarily",
        new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.operation_failed)),
    )
    mute = mocker.patch.object(antispam.ModerationService, "mute_user", new=AsyncMock())
    get_redis = mocker.patch.object(antispam, "get_redis")
    get_detector = mocker.patch.object(antispam, "get_detector")
    log_error = mocker.patch.object(antispam.logger, "error")

    await antispam._apply_immediate_punishment(
        message,
        bot,
        {"confidence": 0.95, "reasons": ["rule:url"]},
        message_type=SpamMessageType.text,
    )

    message.delete.assert_awaited_once()
    ban.assert_awaited_once()
    mute.assert_not_awaited()
    log_error.assert_called_once_with("处罚垃圾用户失败: operation_failed")
    get_redis.assert_not_called()
    get_detector.assert_not_called()
    message.answer.assert_not_awaited()


async def test_route_spam_detection_uses_review_when_enabled(mocker) -> None:
    message = _message()
    bot = MagicMock()
    result = {"confidence": 0.91, "reasons": ["rule:url"]}
    group = SimpleNamespace(spam_confirm_enabled=True)
    review = mocker.patch.object(antispam, "_handle_spam_with_review", new=AsyncMock())
    immediate = mocker.patch.object(antispam, "_apply_immediate_punishment", new=AsyncMock())

    await antispam._route_spam_detection(
        message,
        bot,
        result,
        group,
        message_type=SpamMessageType.sticker,
        recognized_text="recognized text",
    )

    review.assert_awaited_once_with(
        message,
        bot,
        result,
        message_type=SpamMessageType.sticker,
        recognized_text="recognized text",
    )
    immediate.assert_not_awaited()


async def test_route_spam_detection_uses_immediate_when_disabled(mocker) -> None:
    message = _message()
    bot = MagicMock()
    result = {"confidence": 0.91, "reasons": ["rule:url"]}
    group = SimpleNamespace(spam_confirm_enabled=False)
    review = mocker.patch.object(antispam, "_handle_spam_with_review", new=AsyncMock())
    immediate = mocker.patch.object(antispam, "_apply_immediate_punishment", new=AsyncMock())

    await antispam._route_spam_detection(
        message, bot, result, group, message_type=SpamMessageType.edited_text
    )

    immediate.assert_awaited_once_with(
        message,
        bot,
        result,
        message_type=SpamMessageType.edited_text,
        recognized_text=None,
    )
    review.assert_not_awaited()
