"""message 管理员豁免与匿名管理员回归测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam

pytestmark = pytest.mark.unit

CHAT_ID = -100123


def _message(*, sender_chat=None, text=None):
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID, type="supergroup", title="Test")
    message.from_user = SimpleNamespace(id=42, username=None)
    message.sender_chat = sender_chat
    message.text = text
    message.caption = None
    message.message_id = 1
    message.photo = []
    return message


async def test_anonymous_photo_is_skipped_before_group_lookup(mocker) -> None:
    """匿名管理员图片消息（sender_chat==chat）在前置 is_anonymous_admin 短路，不进检测。

    回归 on_photo_message 缺前置 is_anonymous_admin 的漏点（其它 10 处媒体处理器已有）。
    """
    message = _message(sender_chat=SimpleNamespace(id=CHAT_ID))
    bot = MagicMock()

    mocker.patch.object(antispam.settings, "admin_ids", [])
    permission = mocker.patch.object(
        antispam, "check_admin_permission_by_id", new=AsyncMock(return_value=False)
    )
    get_group = mocker.patch.object(antispam.GroupRepository, "get_or_create", new=AsyncMock())

    await antispam.on_photo_message(message, bot)

    # 匿名前置短路：不查权限、不查群组
    permission.assert_not_awaited()
    get_group.assert_not_awaited()
