"""on_webapp_data 身份边界与异常路径测试（3c1-5）。

验证 WebApp 回调的身份校验与异常路径不泄露、不跨用户操作：
- from_user != payload user_id → 拒绝，不操作任何 Redis/验证状态，错误消息发给真实发送者
- 异常路径不依据未鉴权 payload 清除验证状态（防跨用户 DoS）

签名/token/状态机底层由真机覆盖；本测试聚焦 handler 层身份边界。
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import verification as handler

pytestmark = pytest.mark.unit

CLICKER_ID = 42
VICTIM_ID = 999
CHAT_ID = -100


def _patch_i18n(mocker) -> MagicMock:
    """mock get_resolver/get_translator，返回固定 localizer（点击者显式偏好）。"""
    resolver = AsyncMock()
    resolver.for_user.return_value = "zh-Hans"
    mocker.patch.object(handler, "get_resolver", return_value=resolver)

    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}>"
    translator = MagicMock()
    translator.for_locale.return_value = localizer
    mocker.patch.object(handler, "get_translator", return_value=translator)
    return localizer


def _patch_state(mocker) -> tuple[AsyncMock, AsyncMock]:
    """mock VerificationService + approve/restore + get_redis（均不期望被调用）。"""
    service = AsyncMock()
    mocker.patch.object(handler, "VerificationService", return_value=service)
    approve = mocker.patch.object(handler, "approve_join_request", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "restore_user_permissions", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "get_redis", return_value=AsyncMock())
    return service, approve


def _webapp_message(payload: dict) -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=CLICKER_ID)
    message.web_app_data = MagicMock(data=json.dumps(payload))
    return message


async def test_from_user_mismatch_rejects_without_state_change(mocker) -> None:
    """from_user != payload user_id → 不操作状态，错误消息发给真实发送者（非 payload user）。"""
    _patch_i18n(mocker)
    service, approve = _patch_state(mocker)

    payload = {
        "action": "captcha_success",
        "provider": "turnstile",
        "chat_id": CHAT_ID,
        "user_id": VICTIM_ID,  # ≠ from_user.id
        "verify_token": "abc",
        "signature": "def",
        "timestamp": int(time.time()),
    }
    message = _webapp_message(payload)
    bot = AsyncMock()

    await handler.on_webapp_data(message, bot)

    # 身份不符：不操作任何验证状态、不查 token
    service.clear_verification.assert_not_awaited()
    approve.assert_not_awaited()
    handler.restore_user_permissions.assert_not_awaited()
    handler.get_redis.assert_not_called()
    # 错误消息发给真实发送者（from_user.id），而非 payload user_id
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == CLICKER_ID


async def test_exception_does_not_clear_state_via_payload(mocker) -> None:
    """异常路径不依据未鉴权 payload 清状态（防跨用户 DoS）。

    身份相符但 payload 缺字段触发异常 → 外层 except 只通知 from_user，不清状态。
    原逻辑会用 payload chat/user_id 调 clear_verification，可被构造为攻击。
    """
    _patch_i18n(mocker)
    service, approve = _patch_state(mocker)

    # payload 缺 verify_token（KeyError 在身份校验通过后、签名验证前触发）
    payload = {
        "action": "captcha_success",
        "provider": "turnstile",
        "chat_id": CHAT_ID,
        "user_id": CLICKER_ID,  # 身份相符，继续解析
        # "verify_token" 故意缺失
        "signature": "def",
        "timestamp": int(time.time()),
    }
    message = _webapp_message(payload)
    bot = AsyncMock()

    await handler.on_webapp_data(message, bot)

    # 异常不清状态（旧逻辑会清 payload 指向的状态）
    service.clear_verification.assert_not_awaited()
    approve.assert_not_awaited()
    # 通用错误消息发给真实发送者
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == CLICKER_ID
