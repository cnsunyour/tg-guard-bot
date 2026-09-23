"""宵禁中间件管理员豁免测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.types import Message

from src.bot.middlewares import curfew

pytestmark = pytest.mark.unit


async def test_anonymous_message_bypasses_curfew(mocker) -> None:
    """匿名管理员消息（sender_chat==chat）跳过宵禁限制"""
    event = MagicMock(spec=Message)
    event.chat = SimpleNamespace(type="supergroup", id=-100123)
    event.from_user = SimpleNamespace(id=1087968824)
    event.sender_chat = SimpleNamespace(id=-100123)
    # MagicMock 未显式设置的属性为 truthy，service message 字段必须显式置 None
    event.new_chat_members = None
    event.left_chat_member = None

    bot = MagicMock(spec=Bot)
    permission = mocker.patch.object(
        curfew, "check_admin_permission", new=AsyncMock(return_value=True)
    )
    get_group = mocker.patch.object(curfew.GroupRepository, "get", new=AsyncMock())
    handler = AsyncMock(return_value="handled")

    result = await curfew.CurfewMiddleware()(handler, event, {"bot": bot})

    assert result == "handled"
    permission.assert_awaited_once_with(event, bot)
    get_group.assert_not_awaited()


@pytest.mark.parametrize("field", ["new_chat_members", "left_chat_member"])
async def test_membership_service_message_bypasses_curfew(mocker, field: str) -> None:
    """入群 / 退群 service message 放行：outer 化后不得在宵禁期间删掉新成员的入群通知。"""
    event = MagicMock(spec=Message)
    event.chat = SimpleNamespace(type="supergroup", id=-100123)
    event.from_user = SimpleNamespace(id=42)
    event.sender_chat = None
    event.new_chat_members = None
    event.left_chat_member = None
    setattr(event, field, [SimpleNamespace(id=42)])

    permission = mocker.patch.object(
        curfew, "check_admin_permission", new=AsyncMock(return_value=False)
    )
    get_group = mocker.patch.object(curfew.GroupRepository, "get", new=AsyncMock())
    handler = AsyncMock(return_value="handled")

    result = await curfew.CurfewMiddleware()(handler, event, {"bot": MagicMock(spec=Bot)})

    assert result == "handled"
    permission.assert_not_awaited()
    get_group.assert_not_awaited()
