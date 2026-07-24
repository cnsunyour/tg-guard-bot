"""举报按钮权限校验回归测试

锁住 on_report_approve / on_report_reject 必须严格校验实际点击者
``callback.from_user``，而非 Bot 发送的 ``callback.message`` —— 后者的
``from_user`` 是 Bot 自身，曾导致任意成员可绕过权限执行封禁/拒绝。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message

from src.bot.handlers import moderation


@pytest.mark.unit
@pytest.mark.parametrize(
    ("handler", "callback_data", "process_name", "denial_text"),
    [
        (
            moderation.on_report_approve,
            "report_approve:123",
            "_process_report_approval",
            "❌ 只有管理员可以接受举报",
        ),
        (
            moderation.on_report_reject,
            "report_reject:123",
            "_process_report_rejection",
            "❌ 只有管理员可以拒绝举报",
        ),
    ],
)
async def test_report_callback_rejects_non_admin_clicker(
    handler,
    callback_data,
    process_name,
    denial_text,
) -> None:
    """权限检查必须使用点击者 ID，拒绝后不得执行举报处理"""
    chat_id = -1001234567890
    clicker_id = 100200300
    bot_user_id = 999000999  # callback.message 的发送者（Bot 自身）

    callback = MagicMock(spec=CallbackQuery)
    callback.data = callback_data
    callback.answer = AsyncMock()
    callback.from_user = MagicMock()
    callback.from_user.id = clicker_id

    message = MagicMock(spec=Message)
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.from_user = MagicMock()
    message.from_user.id = bot_user_id
    callback.message = message

    bot = AsyncMock()
    permission_check = AsyncMock(return_value=False)
    process = AsyncMock()

    with (
        patch.object(moderation, "check_admin_permission_strict", new=permission_check),
        patch.object(moderation, process_name, new=process),
    ):
        await handler(callback, bot)

    # 核心断言：权限检查用的是点击者 ID，而非 Bot 的 ID
    permission_check.assert_awaited_once_with(bot, chat_id, clicker_id)
    callback.answer.assert_awaited_once_with(denial_text, show_alert=True)
    process.assert_not_awaited()
