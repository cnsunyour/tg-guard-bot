"""批量删除消息（deleteMessages 分批）服务测试

覆盖 ``ModerationService.delete_messages_before/after/range`` 改用 Bot API
deleteMessages 批量删除后的核心契约：
- 按 Telegram 100 条上限分批，批成功整批计入成功（幂等口径）
- ``TelegramBadRequest`` 与瞬态错误（``TelegramNetworkError``/
  ``TelegramServerError``）降级逐条；其余 ``TelegramAPIError``（请求级
  错误）整批计失败，不降级
- 往前删除非正消息 ID 截断、范围删除起止归一化、空批次不发请求
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from src.repositories.audit_repo import AuditRepository
from src.services.moderation import ModerationService

pytestmark = pytest.mark.unit

CHAT_ID = -1001234567890
OPERATOR_ID = 7


def _make_bot() -> MagicMock:
    """构造带批量/逐条删除方法的 mock Bot（默认全部成功）"""
    bot = MagicMock(spec=Bot)
    bot.delete_messages = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _fake_method() -> MagicMock:
    """aiogram 异常构造所需的 method 占位"""
    return MagicMock()


async def test_exactly_100_ids_form_single_batch() -> None:
    """恰好 100 条只发一批，且不触发逐条删除"""
    bot = _make_bot()

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=100, operator_id=OPERATOR_ID
        )

    assert result == (100, 0)
    bot.delete_messages.assert_awaited_once_with(chat_id=CHAT_ID, message_ids=list(range(1, 101)))
    bot.delete_message.assert_not_awaited()


async def test_101_ids_split_into_two_batches() -> None:
    """101 条拆成 100 + 1 两批，顺序保持升序"""
    bot = _make_bot()

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=101, operator_id=OPERATOR_ID
        )

    assert result == (101, 0)
    assert bot.delete_messages.await_count == 2
    assert [call.kwargs["message_ids"] for call in bot.delete_messages.await_args_list] == [
        list(range(1, 101)),
        [101],
    ]


async def test_network_error_falls_back_to_individual_deletes() -> None:
    """瞬态网络错误降级逐条：逐条可恢复并继续删除其余消息（而非整批丢弃）"""
    bot = _make_bot()
    bot.delete_messages.side_effect = TelegramNetworkError(
        method=_fake_method(), message="connection reset"
    )

    # 逐条结果按序：1 成功、2 抖动失败、3 成功——比整批计失败的 1/3 多删 1 条
    bot.delete_message = AsyncMock(
        side_effect=[
            True,
            TelegramNetworkError(method=_fake_method(), message="connection reset"),
            True,
        ]
    )

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=3, operator_id=OPERATOR_ID
        )

    assert result == (2, 1)
    bot.delete_messages.assert_awaited_once()
    assert [call.kwargs["message_id"] for call in bot.delete_message.await_args_list] == [1, 2, 3]


async def test_bad_request_falls_back_to_individual_deletes() -> None:
    """批量 400 时逐条定位：单条失败不影响其余消息的删除与统计"""
    bot = _make_bot()
    bot.delete_messages.side_effect = TelegramBadRequest(
        method=_fake_method(), message="message can't be deleted"
    )

    # 逐条结果按序：1 成功、2 失败（不可删）、3 成功
    bot.delete_message = AsyncMock(
        side_effect=[
            True,
            TelegramBadRequest(method=_fake_method(), message="message can't be deleted"),
            True,
        ]
    )

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=3, operator_id=OPERATOR_ID
        )

    assert result == (2, 1)
    bot.delete_messages.assert_awaited_once()
    assert [call.kwargs["message_id"] for call in bot.delete_message.await_args_list] == [1, 2, 3]


async def test_request_level_api_error_counts_whole_batch_without_fallback() -> None:
    """权限级错误（403）整批计失败，不降级逐条制造注定失败的请求"""
    bot = _make_bot()
    bot.delete_messages.side_effect = TelegramForbiddenError(
        method=_fake_method(), message="bot is not an administrator"
    )

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=3, operator_id=OPERATOR_ID
        )

    assert result == (0, 3)
    bot.delete_message.assert_not_awaited()


async def test_unexpected_exception_falls_back_to_individual() -> None:
    """非 Telegram 异常保持 best-effort 口径，降级逐条"""
    bot = _make_bot()
    bot.delete_messages.side_effect = RuntimeError("unexpected")

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=2, operator_id=OPERATOR_ID
        )

    assert result == (2, 0)
    assert bot.delete_message.await_count == 2


async def test_before_stops_at_non_positive_message_id() -> None:
    """往前删除遇非正 ID 截断，不把无效 ID 发给 Telegram"""
    bot = _make_bot()

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_before(
            bot=bot, chat_id=CHAT_ID, start_message_id=2, count=5, operator_id=OPERATOR_ID
        )

    assert result == (2, 0)
    bot.delete_messages.assert_awaited_once_with(chat_id=CHAT_ID, message_ids=[2, 1])


async def test_before_with_zero_start_sends_nothing() -> None:
    """起始消息 ID 非正时直接空批次，不发请求"""
    bot = _make_bot()

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_before(
            bot=bot, chat_id=CHAT_ID, start_message_id=0, count=5, operator_id=OPERATOR_ID
        )

    assert result == (0, 0)
    bot.delete_messages.assert_not_awaited()
    bot.delete_message.assert_not_awaited()


async def test_range_swaps_endpoints_and_logs_audit() -> None:
    """起止 ID 反向时先归一化再升序分批；审计日志保持既有字段"""
    bot = _make_bot()
    audit_log = AsyncMock()

    with patch.object(AuditRepository, "log_action", new=audit_log):
        result = await ModerationService.delete_messages_range(
            bot=bot,
            chat_id=CHAT_ID,
            start_message_id=5,
            end_message_id=3,
            operator_id=OPERATOR_ID,
        )

    assert result == (3, 0)
    bot.delete_messages.assert_awaited_once_with(chat_id=CHAT_ID, message_ids=[3, 4, 5])
    assert audit_log.await_args is not None
    assert audit_log.await_args.kwargs["details"] == {
        "start_message_id": 3,
        "end_message_id": 5,
        "success": 3,
        "failed": 0,
    }


async def test_zero_count_does_not_send_empty_batch() -> None:
    """count 为 0 时不调用 deleteMessages，返回空统计"""
    bot = _make_bot()

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=0, operator_id=OPERATOR_ID
        )

    assert result == (0, 0)
    bot.delete_messages.assert_not_awaited()
    bot.delete_message.assert_not_awaited()


async def test_mixed_batches_aggregate_counts() -> None:
    """多批混合结果：首批整批失败（请求级错误）+ 次批成功，统计累加"""
    bot = _make_bot()
    bot.delete_messages.side_effect = [
        TelegramForbiddenError(method=_fake_method(), message="kicked"),
        True,
    ]

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=101, operator_id=OPERATOR_ID
        )

    assert result == (1, 100)
    assert bot.delete_messages.await_count == 2
    bot.delete_message.assert_not_awaited()


async def test_retry_after_exhausted_counts_whole_batch_without_fallback() -> None:
    """429 重试耗尽后抛到业务层：按请求级错误整批计失败，不降级逐条"""
    bot = _make_bot()
    bot.delete_messages.side_effect = TelegramRetryAfter(
        method=_fake_method(), message="Too many requests", retry_after=30
    )

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_after(
            bot=bot, chat_id=CHAT_ID, start_message_id=1, count=3, operator_id=OPERATOR_ID
        )

    assert result == (0, 3)
    bot.delete_message.assert_not_awaited()


async def test_before_splits_batches_in_descending_order() -> None:
    """往前删除跨批：150 条拆成 100 + 50，批内 ID 递减"""
    bot = _make_bot()

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_before(
            bot=bot, chat_id=CHAT_ID, start_message_id=250, count=150, operator_id=OPERATOR_ID
        )

    assert result == (150, 0)
    assert bot.delete_messages.await_count == 2
    assert [call.kwargs["message_ids"] for call in bot.delete_messages.await_args_list] == [
        list(range(250, 150, -1)),
        list(range(150, 100, -1)),
    ]


async def test_range_splits_batches_in_ascending_order() -> None:
    """范围删除跨批：101 条拆成 100 + 1，批内 ID 递增"""
    bot = _make_bot()

    with patch.object(AuditRepository, "log_action", new=AsyncMock()):
        result = await ModerationService.delete_messages_range(
            bot=bot,
            chat_id=CHAT_ID,
            start_message_id=1,
            end_message_id=101,
            operator_id=OPERATOR_ID,
        )

    assert result == (101, 0)
    assert bot.delete_messages.await_count == 2
    assert [call.kwargs["message_ids"] for call in bot.delete_messages.await_args_list] == [
        list(range(1, 101)),
        [101],
    ]
