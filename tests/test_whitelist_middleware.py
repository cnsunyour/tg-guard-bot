"""群组白名单中间件（WhitelistMiddleware）测试。

覆盖 outer 注册后新增的边界：入群 / 退群 service message 放行，不重复触发退群。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.types import Message

from src.bot.middlewares import whitelist

pytestmark = pytest.mark.unit

CHAT_ID = -1001234567890


def _message(**overrides) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=CHAT_ID, type="supergroup", title="Test")
    msg.new_chat_members = None
    msg.left_chat_member = None
    for key, value in overrides.items():
        setattr(msg, key, value)
    return msg


def _data() -> dict:
    localizer = MagicMock()
    localizer.t = MagicMock(return_value="unauthorized")
    bot = MagicMock(spec=Bot)
    bot.send_message = AsyncMock()
    bot.leave_chat = AsyncMock()
    return {"bot": bot, "localizer": localizer}


async def test_join_service_message_passes_without_group_lookup(mocker) -> None:
    """new_chat_members 直接放行：退群由 my_chat_member 事件负责，避免重复 leave_chat。"""
    get_group = mocker.patch.object(whitelist.GroupRepository, "get_by_id", new=AsyncMock())
    handler = AsyncMock(return_value="handled")
    data = _data()

    result = await whitelist.WhitelistMiddleware()(
        handler, _message(new_chat_members=[SimpleNamespace(id=1)]), data
    )

    assert result == "handled"
    get_group.assert_not_awaited()
    data["bot"].leave_chat.assert_not_awaited()


async def test_left_service_message_passes_without_group_lookup(mocker) -> None:
    get_group = mocker.patch.object(whitelist.GroupRepository, "get_by_id", new=AsyncMock())
    handler = AsyncMock(return_value="handled")

    result = await whitelist.WhitelistMiddleware()(
        handler, _message(left_chat_member=SimpleNamespace(id=1)), _data()
    )

    assert result == "handled"
    get_group.assert_not_awaited()


async def test_regular_message_in_unlisted_group_leaves(mocker) -> None:
    """普通消息在非白名单群：发提示 + 退群 + 阻断（既有行为不变）。"""
    mocker.patch.object(whitelist.GroupRepository, "get_by_id", new=AsyncMock(return_value=None))
    handler = AsyncMock(return_value="handled")
    data = _data()

    result = await whitelist.WhitelistMiddleware()(handler, _message(), data)

    assert result is None
    handler.assert_not_awaited()
    data["bot"].leave_chat.assert_awaited_once_with(CHAT_ID)


async def test_regular_message_in_whitelisted_group_passes(mocker) -> None:
    mocker.patch.object(
        whitelist.GroupRepository,
        "get_by_id",
        new=AsyncMock(return_value=SimpleNamespace(is_whitelisted=True)),
    )
    handler = AsyncMock(return_value="handled")
    data = _data()

    result = await whitelist.WhitelistMiddleware()(handler, _message(), data)

    assert result == "handled"
    data["bot"].leave_chat.assert_not_awaited()
