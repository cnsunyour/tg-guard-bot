"""CAS 调用链集成测试"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message

import src.bot.handlers.verification as verification
import src.bot.middlewares.cas_check as cas_check_module
from src.services.cas_service import CASCheckResult


def _cas_result(
    user_id: int,
    *,
    is_banned: bool,
    offenses: int = 0,
    error: str | None = None,
    cached: bool = True,
) -> CASCheckResult:
    return CASCheckResult(
        is_banned=is_banned,
        user_id=user_id,
        offenses=offenses,
        error=error,
        cached=cached,
    )


def _make_message(user_id: int = 42, chat_id: int = -1001) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": datetime.now(UTC),
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from_user": {"id": user_id, "is_bot": False, "first_name": "User"},
            "text": "hello",
        }
    )


def _make_join_request(user_id: int = 42, chat_id: int = -1001) -> ChatJoinRequest:
    return ChatJoinRequest.model_validate(
        {
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from_user": {"id": user_id, "is_bot": False, "first_name": "User"},
            "user_chat_id": user_id,
            "date": datetime.now(UTC),
        }
    )


def _make_join_update(
    user_id: int = 42,
    chat_id: int = -1001,
    inviter_id: int = 7,
) -> ChatMemberUpdated:
    return ChatMemberUpdated.model_validate(
        {
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from_user": {"id": inviter_id, "is_bot": False, "first_name": "Inviter"},
            "date": datetime.now(UTC),
            "old_chat_member": {
                "status": "left",
                "user": {"id": user_id, "is_bot": False, "first_name": "User"},
            },
            "new_chat_member": {
                "status": "member",
                "user": {"id": user_id, "is_bot": False, "first_name": "User"},
            },
        }
    )


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.id = 9001
    bot.ban_chat_member = AsyncMock()
    bot.restrict_chat_member = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=321))
    return bot


@pytest.mark.asyncio
async def test_cas_check_middleware_banned_user_triggers_expected_side_effects(
    monkeypatch, mock_bot
):
    event = _make_message()
    handler = AsyncMock()
    delete_mock = AsyncMock()
    service = SimpleNamespace(
        check_user=AsyncMock(return_value=_cas_result(42, is_banned=True, offenses=3))
    )

    monkeypatch.setattr(cas_check_module.settings, "cas_enabled", True)
    monkeypatch.setattr(cas_check_module.settings, "admin_ids", [])
    monkeypatch.setattr(cas_check_module.PermissionCache, "is_admin", AsyncMock(return_value=False))

    with (
        patch.object(Message, "delete", delete_mock),
        patch.object(cas_check_module, "get_cas_service", return_value=service),
        patch.object(cas_check_module.AuditRepository, "log_action", new=AsyncMock()) as log_action,
        patch.object(cas_check_module, "auto_delete_message", new=AsyncMock()) as auto_delete,
    ):
        result = await cas_check_module.CASCheckMiddleware()(handler, event, {"bot": mock_bot})

    assert result is None
    delete_mock.assert_awaited_once()
    mock_bot.ban_chat_member.assert_awaited_once_with(chat_id=-1001, user_id=42)
    log_action.assert_awaited_once_with(
        group_id=-1001,
        operator_id=9001,
        action="cas_ban_on_message",
        target_user_id=42,
        details={"offenses": 3},
    )
    mock_bot.send_message.assert_awaited_once_with(chat_id=-1001, text=ANY, parse_mode="HTML")
    auto_delete.assert_awaited_once()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_cas_check_middleware_snapshot_unavailable_fails_open(monkeypatch, mock_bot):
    event = _make_message()
    handler_result = object()
    handler = AsyncMock(return_value=handler_result)
    delete_mock = AsyncMock()
    service = SimpleNamespace(
        check_user=AsyncMock(
            return_value=_cas_result(
                42,
                is_banned=False,
                error="snapshot_unavailable",
                cached=False,
            )
        )
    )

    monkeypatch.setattr(cas_check_module.settings, "cas_enabled", True)
    monkeypatch.setattr(cas_check_module.settings, "admin_ids", [])
    monkeypatch.setattr(cas_check_module.PermissionCache, "is_admin", AsyncMock(return_value=False))

    with (
        patch.object(Message, "delete", delete_mock),
        patch.object(cas_check_module, "get_cas_service", return_value=service),
        patch.object(cas_check_module.AuditRepository, "log_action", new=AsyncMock()) as log_action,
        patch.object(cas_check_module, "auto_delete_message", new=AsyncMock()) as auto_delete,
    ):
        result = await cas_check_module.CASCheckMiddleware()(handler, event, {"bot": mock_bot})

    assert result is handler_result
    handler.assert_awaited_once()
    delete_mock.assert_not_awaited()
    mock_bot.ban_chat_member.assert_not_awaited()
    log_action.assert_not_awaited()
    auto_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_join_request_banned_user_triggers_expected_side_effects(monkeypatch, mock_bot):
    event = _make_join_request()
    service = SimpleNamespace(
        check_user=AsyncMock(return_value=_cas_result(42, is_banned=True, offenses=5))
    )

    monkeypatch.setattr(verification.settings, "cas_enabled", True)

    with (
        patch.object(verification, "get_cas_service", return_value=service),
        patch.object(verification, "decline_join_request", new=AsyncMock()) as decline,
        patch.object(verification, "check_user_spam_info", new=AsyncMock()) as spam_check,
        patch.object(verification.AuditRepository, "log_action", new=AsyncMock()) as log_action,
    ):
        await verification.on_join_request(event, mock_bot)

    decline.assert_awaited_once_with(mock_bot, -1001, 42)
    mock_bot.ban_chat_member.assert_awaited_once_with(chat_id=-1001, user_id=42)
    log_action.assert_awaited_once_with(
        group_id=-1001,
        operator_id=9001,
        action="cas_ban_on_join_request",
        target_user_id=42,
        details={"offenses": 5},
    )
    spam_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_join_request_snapshot_unavailable_fails_open(monkeypatch, mock_bot):
    event = _make_join_request()
    service = SimpleNamespace(
        check_user=AsyncMock(
            return_value=_cas_result(
                42,
                is_banned=False,
                error="snapshot_unavailable",
                cached=False,
            )
        )
    )

    monkeypatch.setattr(verification.settings, "cas_enabled", True)

    with (
        patch.object(verification, "get_cas_service", return_value=service),
        patch.object(verification, "decline_join_request", new=AsyncMock()) as decline,
        patch.object(
            verification,
            "check_user_spam_info",
            new=AsyncMock(return_value=True),
        ) as spam_check,
        patch.object(verification.AuditRepository, "log_action", new=AsyncMock()) as log_action,
    ):
        await verification.on_join_request(event, mock_bot)

    spam_check.assert_awaited_once_with(mock_bot, -1001, 42, "User", mode="join_request")
    decline.assert_not_awaited()
    mock_bot.ban_chat_member.assert_not_awaited()
    log_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_user_join_banned_user_triggers_expected_side_effects(monkeypatch, mock_bot):
    event = _make_join_update()
    service = SimpleNamespace(
        check_user=AsyncMock(return_value=_cas_result(42, is_banned=True, offenses=7))
    )

    monkeypatch.setattr(verification.settings, "cas_enabled", True)
    monkeypatch.setattr(verification.PermissionCache, "is_admin", AsyncMock(return_value=False))

    with (
        patch.object(verification, "get_cas_service", return_value=service),
        patch.object(verification, "check_user_spam_info", new=AsyncMock()) as spam_check,
        patch.object(verification.AuditRepository, "log_action", new=AsyncMock()) as log_action,
        patch.object(verification, "auto_delete_message", new=AsyncMock()) as auto_delete,
    ):
        await verification.on_user_join(event, mock_bot)

    mock_bot.restrict_chat_member.assert_awaited_once()
    mock_bot.ban_chat_member.assert_awaited_once_with(chat_id=-1001, user_id=42)
    log_action.assert_awaited_once_with(
        group_id=-1001,
        operator_id=9001,
        action="cas_ban_on_join",
        target_user_id=42,
        details={"offenses": 7},
    )
    mock_bot.send_message.assert_awaited_once_with(chat_id=-1001, text=ANY)
    auto_delete.assert_awaited_once()
    spam_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_user_join_snapshot_unavailable_fails_open(monkeypatch, mock_bot):
    event = _make_join_update()
    service = SimpleNamespace(
        check_user=AsyncMock(
            return_value=_cas_result(
                42,
                is_banned=False,
                error="snapshot_unavailable",
                cached=False,
            )
        )
    )

    monkeypatch.setattr(verification.settings, "cas_enabled", True)
    monkeypatch.setattr(verification.PermissionCache, "is_admin", AsyncMock(return_value=False))

    with (
        patch.object(verification, "get_cas_service", return_value=service),
        patch.object(
            verification,
            "check_user_spam_info",
            new=AsyncMock(return_value=True),
        ) as spam_check,
        patch.object(verification.AuditRepository, "log_action", new=AsyncMock()) as log_action,
        patch.object(verification, "auto_delete_message", new=AsyncMock()) as auto_delete,
    ):
        await verification.on_user_join(event, mock_bot)

    mock_bot.restrict_chat_member.assert_awaited_once()
    spam_check.assert_awaited_once_with(mock_bot, -1001, 42, "User", mode="join")
    mock_bot.ban_chat_member.assert_not_awaited()
    log_action.assert_not_awaited()
    auto_delete.assert_not_awaited()
