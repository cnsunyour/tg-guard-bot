"""captcha waiting session 绑定与滚动发布兼容测试（P2.2）。

覆盖：
- 点击输入按钮时由 Lua 原子校验 deadline/main/recovery，并写入 session-bound waiting；
- session 切换后的新格式/旧格式残留均不进入 verify_answer，更不会处罚；
- 旧纯 message_id 仅在仍匹配当前 recovery UI 时兼容；
- handler 将捕获的 deadline 传给 verify_answer，关闭校验后的 session 切换窗口。
"""

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.bot.handlers import verification as handler
from src.core.redis import RedisKeys
from src.services.verification import VerifyResult
from src.services.verification_recovery import VerificationClearToken

pytestmark = pytest.mark.unit

CHAT_ID = -100
USER_ID = 42
MESSAGE_ID = 9876
DEADLINE = "session-a:1120000"


def _patch_i18n(mocker) -> None:
    """mock get_resolver/get_translator（for_user + for_private_from_group）。"""
    resolver = AsyncMock()
    resolver.for_user.return_value = "zh-Hans"
    resolver.for_private_from_group.return_value = "zh-Hans"
    mocker.patch.object(handler, "get_resolver", return_value=resolver)

    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kwargs: key
    translator = MagicMock()
    translator.for_locale.return_value = localizer
    mocker.patch.object(handler, "get_translator", return_value=translator)


def _callback() -> MagicMock:
    callback = MagicMock()
    callback.data = f"verify_captcha_input:{CHAT_ID}:{USER_ID}"
    callback.from_user = MagicMock(id=USER_ID)
    callback.message = MagicMock(message_id=MESSAGE_ID)
    callback.answer = AsyncMock()
    return callback


def _message(text: str = "abcd") -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=USER_ID, full_name="Alice")
    message.text = text
    message.web_app_data = None
    message.answer = AsyncMock()
    return message


async def test_input_request_atomically_binds_current_session_and_message(mocker) -> None:
    """Lua 成功返回 session-bound 值后才提示输入；正反索引在同一 Lua 中写入。"""
    _patch_i18n(mocker)
    redis = AsyncMock()
    redis.eval.return_value = f"session-a:{MESSAGE_ID}"
    mocker.patch.object(handler, "get_redis", return_value=redis)
    callback = _callback()

    await handler.on_captcha_input_request(callback)

    redis.eval.assert_awaited_once_with(
        handler._CAPTCHA_WAITING_SET_SCRIPT,
        5,
        RedisKeys.verification_deadline(CHAT_ID, USER_ID),
        RedisKeys.verification(CHAT_ID, USER_ID),
        RedisKeys.verification_recovery(CHAT_ID, USER_ID),
        RedisKeys.captcha_waiting(CHAT_ID, USER_ID),
        RedisKeys.captcha_waiting_user(USER_ID),
        str(MESSAGE_ID),
        str(CHAT_ID),
    )
    callback.answer.assert_awaited_once_with(
        "verification.captcha.input_prompt.toast",
        show_alert=False,
    )


async def test_input_request_without_live_matching_session_reports_expired(mocker) -> None:
    """deadline 缺失/已到期、main 非 captcha 或旧 message_id 均由 Lua 拒绝→expired toast。"""
    _patch_i18n(mocker)
    redis = AsyncMock()
    redis.eval.return_value = 0
    mocker.patch.object(handler, "get_redis", return_value=redis)
    callback = _callback()

    await handler.on_captcha_input_request(callback)

    callback.answer.assert_awaited_once_with(
        "verification.callback.expired.toast",
        show_alert=False,
    )


@pytest.mark.parametrize(
    "waiting_value",
    [
        "old-session:111",  # 新格式但 session 已切换
        "111",  # 旧格式残留
    ],
)
async def test_stale_waiting_is_cleared_without_using_new_session(
    mocker,
    waiting_value: str,
) -> None:
    """新 session 已建立时，新格式旧 session 和旧格式旧 message 均不得误判答案。"""
    _patch_i18n(mocker)
    waiting_key = RedisKeys.captcha_waiting(CHAT_ID, USER_ID)
    waiting_user_key = RedisKeys.captcha_waiting_user(USER_ID)

    redis = AsyncMock()

    async def get_value(key: str) -> str | None:
        return {
            waiting_user_key: str(CHAT_ID),
            waiting_key: waiting_value,
        }.get(key)

    redis.get.side_effect = get_value
    redis.eval.return_value = 1
    mocker.patch.object(handler, "get_redis", return_value=redis)

    service = AsyncMock()
    service.capture_clear_token.return_value = VerificationClearToken(
        state_value="captcha:NEWCODE",
        deadline_value="new-session:1240000",
        recovery_value="message:new-session:initial:join:222",
    )
    mocker.patch.object(handler, "VerificationService", return_value=service)

    message = _message("OLDCODE")
    bot = AsyncMock()

    await handler.on_captcha_text_input(message, bot)

    service.verify_answer.assert_not_awaited()
    service.clear_verification.assert_not_awaited()
    message.answer.assert_not_awaited()
    bot.ban_chat_member.assert_not_awaited()
    redis.eval.assert_awaited_once_with(
        handler._CAPTCHA_WAITING_CLEAR_SCRIPT,
        2,
        waiting_key,
        waiting_user_key,
        waiting_value,
        str(CHAT_ID),
    )


async def test_legacy_message_id_is_accepted_only_for_current_recovery_ui(mocker) -> None:
    """滚动发布旧值若仍对应当前 session/UI，可安全进入答案校验。"""
    _patch_i18n(mocker)
    waiting_key = RedisKeys.captcha_waiting(CHAT_ID, USER_ID)
    waiting_user_key = RedisKeys.captcha_waiting_user(USER_ID)
    legacy_waiting_value = "111"

    redis = AsyncMock()

    async def get_value(key: str) -> str | None:
        return {
            waiting_user_key: str(CHAT_ID),
            waiting_key: legacy_waiting_value,
        }.get(key)

    redis.get.side_effect = get_value
    redis.eval.return_value = 1
    mocker.patch.object(handler, "get_redis", return_value=redis)

    service = AsyncMock()
    service.capture_clear_token.return_value = VerificationClearToken(
        state_value="captcha:ABCD",
        deadline_value=DEADLINE,
        recovery_value="message:session-a:initial:join:111",
    )
    # 用 expired 收束测试，不进入成功恢复或失败处罚等无关分支
    service.verify_answer.return_value = VerifyResult(status="expired")
    mocker.patch.object(handler, "VerificationService", return_value=service)

    message = _message("abcd")
    bot = AsyncMock()

    await handler.on_captcha_text_input(message, bot)

    service.verify_answer.assert_awaited_once_with(
        CHAT_ID,
        USER_ID,
        "abcd",
        expected_deadline_value=DEADLINE,
    )
    bot.ban_chat_member.assert_not_awaited()
    redis.eval.assert_awaited_once_with(
        handler._CAPTCHA_WAITING_CLEAR_SCRIPT,
        2,
        waiting_key,
        waiting_user_key,
        legacy_waiting_value,
        str(CHAT_ID),
    )


async def test_correct_captcha_uses_claimed_flow_without_getting_type(mocker) -> None:
    """correct 的 join_request flow 直接来自 claim，不在成功后再次 GET type。"""
    _patch_i18n(mocker)
    waiting_key = RedisKeys.captcha_waiting(CHAT_ID, USER_ID)
    waiting_user_key = RedisKeys.captcha_waiting_user(USER_ID)
    waiting_value = "session-a:111"

    redis = AsyncMock()

    async def get_value(key: str) -> str | None:
        return {
            waiting_user_key: str(CHAT_ID),
            waiting_key: waiting_value,
        }.get(key)

    redis.get.side_effect = get_value
    redis.eval.return_value = 1
    mocker.patch.object(handler, "get_redis", return_value=redis)

    service = AsyncMock()
    service.is_verification_pending.return_value = True
    service.capture_clear_token.return_value = VerificationClearToken(
        state_value="captcha:ABCD",
        deadline_value=DEADLINE,
        recovery_value="message:session-a:initial:join_request:111",
    )
    service.verify_answer.return_value = VerifyResult(status="correct", flow="join_request")
    mocker.patch.object(handler, "VerificationService", return_value=service)

    approve = mocker.patch.object(handler, "approve_join_request", new=AsyncMock(return_value=True))
    restore = mocker.patch.object(
        handler, "restore_user_permissions", new=AsyncMock(return_value=True)
    )
    message = _message("abcd")
    bot = AsyncMock()
    bot.get_chat.return_value = MagicMock(title="Test Group")

    await handler.on_captcha_text_input(message, bot)

    service.verify_answer.assert_awaited_once_with(
        CHAT_ID, USER_ID, "abcd", expected_deadline_value=DEADLINE
    )
    approve.assert_awaited_once_with(bot, CHAT_ID, USER_ID)
    restore.assert_not_awaited()
    service.clear_verification.assert_not_awaited()
    redis.get.assert_has_awaits([call(waiting_user_key), call(waiting_key)])
    assert redis.get.await_count == 2
