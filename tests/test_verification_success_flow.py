"""选项验证 handler 使用原子 claim 返回 flow 的回归测试（P2.5 correct + wrong 路径）。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import verification as handler
from src.services.verification import VerifyResult
from src.services.verification_recovery import VerificationFlow

pytestmark = pytest.mark.unit

CHAT_ID = -100
USER_ID = 42


def _patch_i18n(mocker) -> None:
    resolver = AsyncMock()
    resolver.for_private_from_group.return_value = "zh-Hans"
    mocker.patch.object(handler, "get_resolver", return_value=resolver)

    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kwargs: key
    translator = MagicMock()
    translator.for_locale.return_value = localizer
    mocker.patch.object(handler, "get_translator", return_value=translator)


async def test_choice_success_uses_claimed_flow_without_getting_type(mocker) -> None:
    """join_request flow 由 verify result 传给成功处理，不再二次访问 Redis type。"""
    _patch_i18n(mocker)
    callback = MagicMock()
    callback.data = f"verify_math:{CHAT_ID}:{USER_ID}:4"
    callback.from_user = MagicMock(id=USER_ID)
    callback.message = MagicMock(message_id=111)
    callback.answer = AsyncMock()
    bot = AsyncMock()

    service = AsyncMock()
    service.verify_choice_answer.return_value = VerifyResult(status="correct", flow="join_request")
    mocker.patch.object(handler, "VerificationService", return_value=service)
    get_redis = mocker.patch.object(handler, "get_redis")
    handle_success = mocker.patch.object(handler, "handle_verification_success", new=AsyncMock())

    await handler.on_choice_verify(callback, bot)

    service.verify_choice_answer.assert_awaited_once_with(CHAT_ID, USER_ID, "math", "4")
    handle_success.assert_awaited_once_with(bot, callback, CHAT_ID, USER_ID, is_join_request=True)
    service.capture_clear_token.assert_not_awaited()
    service.clear_verification.assert_not_awaited()
    get_redis.assert_not_called()


@pytest.mark.parametrize(
    ("flow", "should_decline"),
    [
        ("join", False),
        ("join_request", True),
    ],
)
async def test_choice_wrong_uses_claimed_flow_without_getting_type_or_clearing(
    mocker,
    flow: VerificationFlow,
    should_decline: bool,
) -> None:
    """wrong 直接使用 claim_failure 返回的 flow 处罚，不再访问 type 或事后 clear。"""
    _patch_i18n(mocker)
    callback = MagicMock()
    callback.data = f"verify_math:{CHAT_ID}:{USER_ID}:3"
    callback.from_user = MagicMock(id=USER_ID)
    callback.message = MagicMock(message_id=111)
    callback.answer = AsyncMock()
    bot = AsyncMock()

    service = AsyncMock()
    service.verify_choice_answer.return_value = VerifyResult(status="wrong", flow=flow)
    mocker.patch.object(handler, "VerificationService", return_value=service)
    get_redis = mocker.patch.object(handler, "get_redis")
    decline = mocker.patch.object(handler, "decline_join_request", new=AsyncMock(return_value=True))
    handle_success = mocker.patch.object(handler, "handle_verification_success", new=AsyncMock())

    await handler.on_choice_verify(callback, bot)

    service.verify_choice_answer.assert_awaited_once_with(CHAT_ID, USER_ID, "math", "3")
    service.capture_clear_token.assert_not_awaited()
    service.clear_verification.assert_not_awaited()
    handle_success.assert_not_awaited()
    bot.ban_chat_member.assert_awaited_once()
    if should_decline:
        decline.assert_awaited_once_with(bot, CHAT_ID, USER_ID)
    else:
        decline.assert_not_awaited()
    get_redis.assert_not_called()
