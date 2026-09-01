"""初始验证发送编排测试（_start_initial_verification）。

验证 reserve → prepare → commit → 启 timeout → send → promote 的编排顺序与分支：
- sent 路径：send 成功 → promote 关联真实 message_id
- undelivered 路径：send Forbidden → release preserve_challenge=True

状态机底层（reserve/commit/promote/release/claim）由 test_verification_recovery.py 覆盖；
本测试聚焦 handler 层编排，依赖均 mock。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError

from src.bot.handlers import verification as handler
from src.services.verification import MathChallenge, PreparedChallenge
from src.services.verification_recovery import RecoveryReservation

pytestmark = pytest.mark.unit


def _reservation() -> RecoveryReservation:
    return RecoveryReservation(
        chat_id=-100,
        user_id=42,
        session_id="session-a",
        revision="initial",
        owner_token="owner-a",
        deadline_ms=1_120_000,
        expected_state_value=None,
        initial=True,
    )


def _prepared() -> PreparedChallenge:
    return PreparedChallenge(
        challenge=MathChallenge(expression="2 + 2", choices=(3, 4, 5, 6)),
        state_value="math:4",
    )


def _patch_common(mocker, reservation, send_side_effect=None, send_return=None):
    """mock _start_initial_verification 的全部外部依赖。"""
    resolver = AsyncMock()
    resolver.for_private_from_group.return_value = "zh-Hant"
    mocker.patch.object(handler, "get_resolver", return_value=resolver)

    service = AsyncMock()
    service.commit_challenge.return_value = True
    mocker.patch.object(handler, "VerificationService", return_value=service)
    mocker.patch.object(handler, "new_session_id", return_value="session-a")
    mocker.patch.object(
        handler, "reserve_initial_recovery", new=AsyncMock(return_value=reservation)
    )
    mocker.patch.object(
        handler, "prepare_verification_challenge", new=AsyncMock(return_value=_prepared())
    )
    send_mock = AsyncMock()
    if send_side_effect is not None:
        send_mock.side_effect = send_side_effect
    else:
        send_mock.return_value = send_return or MagicMock(message_id=9876)
    mocker.patch.object(handler, "send_verification_message", new=send_mock)
    # timeout 派发经 dispatch_verification_timeout（内部走 spawn_background_task）：
    # 直接吞掉即可，不创建真实协程，也无需 close
    mocker.patch.object(handler, "dispatch_verification_timeout", side_effect=lambda *a, **kw: None)
    return service


async def test_initial_send_promotes_real_message_id(mocker) -> None:
    """sent 路径：send 成功后 promote_recovery 被以真实 message_id 调用。"""
    reservation = _reservation()
    _patch_common(mocker, reservation, send_return=MagicMock(message_id=9876))
    promote = mocker.patch.object(handler, "promote_recovery", new=AsyncMock(return_value=True))

    bot = AsyncMock()
    group = MagicMock(verification_type="math", verification_timeout=120)

    result = await handler._start_initial_verification(bot, group, -100, 42, "Alice", "join")

    assert result == "sent"
    handler.prepare_verification_challenge.assert_awaited_once_with(
        group, -100, 42, locale="zh-Hant"
    )
    promote.assert_awaited_once_with(reservation, "join", 9876)


async def test_forbidden_releases_to_undelivered(mocker) -> None:
    """undelivered 路径：send 抛 TelegramForbiddenError → release preserve_challenge=True。"""
    reservation = _reservation()
    _patch_common(
        mocker, reservation, send_side_effect=TelegramForbiddenError(MagicMock(), "forbidden")
    )
    release = mocker.patch.object(handler, "release_recovery", new=AsyncMock(return_value=True))

    bot = AsyncMock()
    group = MagicMock(verification_type="math", verification_timeout=120)

    result = await handler._start_initial_verification(bot, group, -100, 42, "Alice", "join")

    assert result == "undelivered"
    release.assert_awaited_once_with(reservation, preserve_challenge=True)


async def test_initial_send_commit_failure_raises(mocker) -> None:
    """commit 失败（reservation 过期/状态被替换）→ 抛 RuntimeError，调用方 except 执行 ban/decline。

    避免返回 busy 被忽略导致用户永久受限（initial reserve 成功后无旧 session timeout 兜底）。
    """
    reservation = _reservation()
    service = _patch_common(mocker, reservation, send_return=MagicMock(message_id=9876))
    service.commit_challenge.return_value = False  # commit 失败
    mocker.patch.object(handler, "promote_recovery", new=AsyncMock(return_value=True))
    release = mocker.patch.object(handler, "release_recovery", new=AsyncMock(return_value=True))

    bot = AsyncMock()
    group = MagicMock(verification_type="math", verification_timeout=120)

    with pytest.raises(RuntimeError, match="commit_challenge 失败"):
        await handler._start_initial_verification(bot, group, -100, 42, "Alice", "join")

    # commit 失败 → except 统一 release(preserve=False)
    release.assert_awaited_once_with(reservation, preserve_challenge=False)
