"""handle_verification_start 成员矩阵 + 恢复链路测试（3c1-4b）。

验证 /start verify_[join_request_]{chat_id} 的成员矩阵分支与恢复链路编排：
- admin → 清 stale + admin 提示
- join + member + undelivered → 恢复（promote 关联 message_id）
- join + member + message → already_sent
- join + left → 清 stale + rejoin
- join_request + left + undelivered → 恢复（合法申请中）
- 恢复 reserve None（并发）→ recovering

依赖均 mock；状态机底层由 test_verification_recovery.py 覆盖。
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import verification as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100
USER_ID = 42


def _mock_message() -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=USER_ID, full_name="Alice")
    message.answer = AsyncMock()
    return message


def _mock_member(status: str, is_member: bool = True) -> MagicMock:
    member = MagicMock()
    member.status = status
    member.is_member = is_member
    return member


def _patch_i18n(mocker, redis_get_map: dict[str, str | None]) -> tuple[MagicMock, AsyncMock]:
    """mock get_resolver/get_translator/get_redis。redis_get_map: key→value 的 get 映射。"""
    resolver = AsyncMock()
    resolver.for_private_from_group.return_value = "zh-Hans"
    mocker.patch.object(handler, "get_resolver", return_value=resolver)

    localizer = MagicMock()
    localizer.t.return_value = "<text>"
    translator = MagicMock()
    translator.for_locale.return_value = localizer
    mocker.patch.object(handler, "get_translator", return_value=translator)

    redis = AsyncMock()

    async def _get(key):
        return redis_get_map.get(key)

    redis.get = _get
    mocker.patch.object(handler, "get_redis", return_value=redis)
    return localizer, redis


def _patch_recovery(
    mocker,
    *,
    reserve_return,
) -> AsyncMock:
    """mock verification_recovery 函数 + VerificationService + send_verification_message。"""
    mocker.patch.object(handler, "new_revision_id", return_value="rev-1")
    mocker.patch.object(handler, "reserve_recovery", new=AsyncMock(return_value=reserve_return))
    mocker.patch.object(handler, "commit_recovery", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "promote_recovery", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "release_recovery", new=AsyncMock(return_value=True))

    service = AsyncMock()
    service.prepare_challenge.return_value = MagicMock(state_value="math:4", auxiliary_state=None)
    mocker.patch.object(handler, "VerificationService", return_value=service)

    mocker.patch.object(
        handler,
        "send_verification_message",
        new=AsyncMock(return_value=MagicMock(message_id=9999)),
    )
    return service


def _mock_bot(member: MagicMock) -> AsyncMock:
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=member)
    chat = MagicMock()
    chat.title = "Test Group"
    bot.get_chat = AsyncMock(return_value=chat)
    return bot


async def test_admin_clears_and_answers(mocker) -> None:
    """admin/creator → 清 stale + admin 提示（不进入恢复链路）。"""
    message = _mock_message()
    bot = _mock_bot(_mock_member("administrator"))
    _patch_i18n(mocker, {})
    service = AsyncMock()
    mocker.patch.object(handler, "VerificationService", return_value=service)
    mocker.patch.object(handler, "reserve_recovery", new=AsyncMock())

    await handler.handle_verification_start(message, bot, CHAT_ID, "join")

    service.clear_verification.assert_awaited_once_with(CHAT_ID, USER_ID)
    message.answer.assert_awaited_once()
    handler.reserve_recovery.assert_not_awaited()  # admin 不进入恢复


async def test_join_member_undelivered_recovers(mocker) -> None:
    """join + member + undelivered → reserve/prepare/commit/send/promote 恢复。"""
    from src.core.redis import RedisKeys

    message = _mock_message()
    bot = _mock_bot(_mock_member("member"))
    redis_map = {
        RedisKeys.verification_recovery(CHAT_ID, USER_ID): "undelivered:session-a",
        RedisKeys.verification_type(CHAT_ID, USER_ID): "join",
        RedisKeys.verification(CHAT_ID, USER_ID): "math:4",
    }
    _patch_i18n(mocker, redis_map)
    reservation = MagicMock(
        chat_id=CHAT_ID,
        user_id=USER_ID,
        session_id="session-a",
        deadline_ms=int(time.time() * 1000) + 120_000,
        expected_state_value="math:4",
    )
    service = _patch_recovery(mocker, reserve_return=reservation)

    await handler.handle_verification_start(message, bot, CHAT_ID, "join")

    handler.reserve_recovery.assert_awaited_once_with(CHAT_ID, USER_ID, "rev-1")
    service.prepare_challenge.assert_awaited_once_with("math", CHAT_ID, USER_ID, locale="zh-Hans")
    handler.promote_recovery.assert_awaited_once()
    handler.send_verification_message.assert_awaited_once()


async def test_join_member_message_already_sent(mocker) -> None:
    """join + member + message → already_sent（不恢复）。"""
    from src.core.redis import RedisKeys

    message = _mock_message()
    bot = _mock_bot(_mock_member("member"))
    redis_map = {
        RedisKeys.verification_recovery(CHAT_ID, USER_ID): "message:session-a:rev:join:9999",
        RedisKeys.verification_type(CHAT_ID, USER_ID): "join",
    }
    _patch_i18n(mocker, redis_map)
    mocker.patch.object(handler, "reserve_recovery", new=AsyncMock())

    await handler.handle_verification_start(message, bot, CHAT_ID, "join")

    handler.reserve_recovery.assert_not_awaited()  # already_sent 不恢复
    message.answer.assert_awaited_once()


async def test_join_left_clears_and_rejoin(mocker) -> None:
    """join + left → 清 stale + rejoin 提示。"""
    from src.core.redis import RedisKeys

    message = _mock_message()
    bot = _mock_bot(_mock_member("left"))
    _patch_i18n(
        mocker,
        {RedisKeys.verification_recovery(CHAT_ID, USER_ID): "undelivered:session-a"},
    )
    service = AsyncMock()
    mocker.patch.object(handler, "VerificationService", return_value=service)
    mocker.patch.object(handler, "reserve_recovery", new=AsyncMock())

    await handler.handle_verification_start(message, bot, CHAT_ID, "join")

    service.clear_verification.assert_awaited_once_with(CHAT_ID, USER_ID)
    handler.reserve_recovery.assert_not_awaited()  # left 不恢复


async def test_join_request_left_undelivered_recovers(mocker) -> None:
    """join_request + left + undelivered → 恢复（left 是合法申请中状态）。"""
    from src.core.redis import RedisKeys

    message = _mock_message()
    bot = _mock_bot(_mock_member("left"))
    redis_map = {
        RedisKeys.verification_recovery(CHAT_ID, USER_ID): "undelivered:session-a",
        RedisKeys.verification_type(CHAT_ID, USER_ID): "join_request",
        RedisKeys.verification(CHAT_ID, USER_ID): "math:4",
    }
    _patch_i18n(mocker, redis_map)
    reservation = MagicMock(
        chat_id=CHAT_ID,
        user_id=USER_ID,
        session_id="session-a",
        deadline_ms=int(time.time() * 1000) + 120_000,
        expected_state_value="math:4",
    )
    _patch_recovery(mocker, reserve_return=reservation)

    await handler.handle_verification_start(message, bot, CHAT_ID, "join_request")

    handler.reserve_recovery.assert_awaited_once()  # left + join_request 可恢复


async def test_recover_reserve_none_recovering(mocker) -> None:
    """恢复时 reserve_recovery 返回 None（并发 busy）→ recovering 提示。"""
    from src.core.redis import RedisKeys

    message = _mock_message()
    bot = _mock_bot(_mock_member("member"))
    redis_map = {
        RedisKeys.verification_recovery(CHAT_ID, USER_ID): "undelivered:session-a",
        RedisKeys.verification_type(CHAT_ID, USER_ID): "join",
        RedisKeys.verification(CHAT_ID, USER_ID): "math:4",
    }
    _patch_i18n(mocker, redis_map)
    _patch_recovery(mocker, reserve_return=None)  # 并发，reserve 失败

    await handler.handle_verification_start(message, bot, CHAT_ID, "join")

    handler.reserve_recovery.assert_awaited_once()
    handler.send_verification_message.assert_not_awaited()  # 未发送
    message.answer.assert_awaited_once()  # recovering 提示
