"""``/spam`` 管理员分支契约测试

覆盖本次修复的核心契约：封禁是 best-effort，不得连带丢掉管理员已明确表态的
「删除消息 + 标注训练样本」——目标已被踢出群（``user_not_in_chat``）曾导致整个
分支被阻断，垃圾消息既不删除也不入训练库。

- 目标已离群/API 故障：仍删消息、仍入库，回复用「未封禁」文案
- 目标是管理员：唯一硬阻断，不删消息也不入库
- ``-d`` 封禁失败时 ``revoke_messages`` 未生效，退化为删除被回复的单条消息
- 自动训练仅在样本成功入库后触发
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from src.bot.handlers import moderation
from src.services.moderation import ModerationErrorCode, ModerationResult


@pytest.fixture(autouse=True)
def _fake_review_lock(mocker):
    """隔离管理员直达 /spam 的同消息处置锁（review_lock 依赖真实 Redis）。"""

    @asynccontextmanager
    async def fake_lock(chat_id: int, message_id: int):
        yield True

    mocker.patch.object(moderation, "review_lock", new=fake_lock)


def _make_message(text: str = "/spam") -> Message:
    """构造一条回复了垃圾消息的群内命令消息。"""
    message = MagicMock(spec=Message)
    message.text = text
    message.chat = MagicMock()
    message.chat.id = -1001234567890
    message.chat.type = "supergroup"
    message.from_user = MagicMock()
    message.from_user.id = 100200300  # 操作者（管理员）
    message.answer = AsyncMock()

    replied = MagicMock(spec=Message)
    replied.message_id = 4321
    replied.text = "低价VPN 加微信xxx"
    replied.caption = None
    replied.content_type = "text"
    replied.from_user = MagicMock()
    replied.from_user.id = 555666777  # 垃圾消息作者
    replied.delete = AsyncMock()
    message.reply_to_message = replied

    return message


def _make_localizer() -> MagicMock:
    localizer = MagicMock()
    localizer.t = MagicMock(side_effect=lambda key, **kw: f"<{key}>")
    return localizer


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_code",
    [
        ModerationErrorCode.user_not_in_chat,
        ModerationErrorCode.verify_user_failed,
        ModerationErrorCode.verify_admin_failed,
        ModerationErrorCode.operation_failed,
    ],
)
async def test_ban_failure_still_deletes_and_trains(error_code) -> None:
    """封禁失败（非 target_is_admin）不得阻断删除与入库。"""
    message = _make_message()
    localizer = _make_localizer()
    upsert_sample = AsyncMock()
    detector = MagicMock()
    detector.check_and_auto_train = AsyncMock(return_value=None)

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(
            moderation.ModerationService,
            "ban_user",
            new=AsyncMock(return_value=ModerationResult(code=error_code)),
        ),
        patch.object(moderation.SpamRepository, "upsert_sample", new=upsert_sample),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
        patch("src.services.spam_detector.get_detector", return_value=detector),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    message.reply_to_message.delete.assert_awaited_once()
    upsert_sample.assert_awaited_once()
    detector.check_and_auto_train.assert_awaited_once()

    # 回复必须如实告知「未封禁」，不得谎报封禁成功
    used_keys = [call.args[0] for call in localizer.t.call_args_list]
    assert "moderation.spam.processed_ban_failed.message" in used_keys
    assert "moderation.spam.processed.message" not in used_keys


@pytest.mark.unit
async def test_ban_success_uses_processed_message() -> None:
    """封禁成功仍走原有成功文案。"""
    message = _make_message()
    localizer = _make_localizer()

    detector = MagicMock()
    detector.check_and_auto_train = AsyncMock(return_value=None)

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(
            moderation.ModerationService,
            "ban_user",
            new=AsyncMock(return_value=ModerationResult()),
        ),
        patch.object(moderation.SpamRepository, "upsert_sample", new=AsyncMock()),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
        patch("src.services.spam_detector.get_detector", return_value=detector),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    used_keys = [call.args[0] for call in localizer.t.call_args_list]
    assert "moderation.spam.processed.message" in used_keys
    assert "moderation.spam.processed_ban_failed.message" not in used_keys


@pytest.mark.unit
async def test_target_is_admin_blocks_everything() -> None:
    """目标是管理员：硬阻断，既不删消息也不写训练样本。"""
    message = _make_message()
    localizer = _make_localizer()
    upsert_sample = AsyncMock()

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(
            moderation.ModerationService,
            "ban_user",
            new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.target_is_admin)),
        ),
        patch.object(moderation.SpamRepository, "upsert_sample", new=upsert_sample),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    message.reply_to_message.delete.assert_not_awaited()
    upsert_sample.assert_not_awaited()


@pytest.mark.unit
async def test_delete_all_skips_single_delete_on_ban_success() -> None:
    """``-d`` 封禁成功时由 revoke_messages 批量删除，不再单独删。"""
    message = _make_message(text="/spam -d")
    localizer = _make_localizer()
    detector = MagicMock()
    detector.check_and_auto_train = AsyncMock(return_value=None)

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(
            moderation.ModerationService,
            "ban_user",
            new=AsyncMock(return_value=ModerationResult()),
        ),
        patch.object(moderation.SpamRepository, "upsert_sample", new=AsyncMock()),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
        patch("src.services.spam_detector.get_detector", return_value=detector),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    message.reply_to_message.delete.assert_not_awaited()


@pytest.mark.unit
async def test_delete_all_falls_back_to_single_delete_on_ban_failure() -> None:
    """``-d`` 封禁失败时 revoke_messages 未生效，至少删掉被回复的这一条。"""
    message = _make_message(text="/spam -d")
    localizer = _make_localizer()
    detector = MagicMock()
    detector.check_and_auto_train = AsyncMock(return_value=None)

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(
            moderation.ModerationService,
            "ban_user",
            new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.user_not_in_chat)),
        ),
        patch.object(moderation.SpamRepository, "upsert_sample", new=AsyncMock()),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
        patch("src.services.spam_detector.get_detector", return_value=detector),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    message.reply_to_message.delete.assert_awaited_once()


@pytest.mark.unit
async def test_auto_train_skipped_when_sample_insert_fails() -> None:
    """样本入库失败时无新增数据，不应触发自动训练。"""
    message = _make_message()
    localizer = _make_localizer()
    detector = MagicMock()
    detector.check_and_auto_train = AsyncMock(return_value=None)

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(
            moderation.ModerationService,
            "ban_user",
            new=AsyncMock(return_value=ModerationResult()),
        ),
        patch.object(
            moderation.SpamRepository,
            "upsert_sample",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
        patch("src.services.spam_detector.get_detector", return_value=detector),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    detector.check_and_auto_train.assert_not_awaited()
    # 入库失败不影响消息删除与回复
    message.reply_to_message.delete.assert_awaited_once()
    message.answer.assert_awaited()


@pytest.mark.unit
async def test_ban_user_called_with_allow_left() -> None:
    """核心回归：必须传 allow_left=True，否则已被踢出的用户会阻断整个流程。"""
    message = _make_message()
    localizer = _make_localizer()
    ban_user = AsyncMock(return_value=ModerationResult())
    detector = MagicMock()
    detector.check_and_auto_train = AsyncMock(return_value=None)

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(moderation.ModerationService, "ban_user", new=ban_user),
        patch.object(moderation.SpamRepository, "upsert_sample", new=AsyncMock()),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
        patch("src.services.spam_detector.get_detector", return_value=detector),
    ):
        await moderation.cmd_spam(message, AsyncMock(), localizer)

    assert ban_user.await_args.kwargs["allow_left"] is True


@pytest.mark.unit
async def test_notspam_admin_trains_under_decision_guard_and_closes_vote() -> None:
    """管理员 /notspam：同消息守卫内训练，退出时关闭该消息的集体投票。

    回归契约（codex recheck）：训练不持锁时并发投票终局可在管理员表态非垃圾
    之后仍执行处罚；guard 兼职"获锁处置后 discard"，训练失败路径同样关闭。
    """
    message = _make_message(text="/notspam")
    localizer = _make_localizer()
    discard = patch.object(moderation, "discard_vote_session", new=AsyncMock())
    guard_discard = discard.start()

    detector = MagicMock()
    detector.check_and_auto_train = AsyncMock(return_value=None)

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(moderation.SpamRepository, "upsert_sample", new=AsyncMock()),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
        patch("src.services.spam_detector.get_detector", return_value=detector),
    ):
        await moderation.cmd_notspam(message, AsyncMock(), localizer)

    # 训练执行 + 退出守卫时关闭投票（reply 的消息 ID 为目标）
    guard_discard.assert_awaited_once_with(message.chat.id, message.reply_to_message.message_id)


@pytest.mark.unit
async def test_notspam_admin_training_failure_still_closes_vote() -> None:
    """训练失败（入库异常）同样关闭投票：管理员已终局表态，不得留下可 +阈 的会话。"""
    message = _make_message(text="/notspam")
    localizer = _make_localizer()
    discard = patch.object(moderation, "discard_vote_session", new=AsyncMock())
    guard_discard = discard.start()

    with (
        patch.object(
            moderation, "check_admin_permission_strict_message", new=AsyncMock(return_value=True)
        ),
        patch.object(
            moderation.SpamRepository,
            "upsert_sample",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch.object(moderation, "auto_delete_message", new=AsyncMock()),
    ):
        await moderation.cmd_notspam(message, AsyncMock(), localizer)

    guard_discard.assert_awaited_once_with(message.chat.id, message.reply_to_message.message_id)

    used_keys = [call.args[0] for call in localizer.t.call_args_list]
    assert "moderation.notspam.failed.message" in used_keys
