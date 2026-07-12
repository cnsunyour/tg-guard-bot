"""入群短窗口消息防护中间件（VerificationGuardMiddleware）测试"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from src.bot.middlewares.verification_guard import VerificationGuardMiddleware
from src.core.redis import RedisKeys


@pytest.fixture
def middleware() -> VerificationGuardMiddleware:
    return VerificationGuardMiddleware()


@pytest.fixture
def mock_bot():
    """模拟 Bot 实例"""
    bot = AsyncMock()
    bot.id = 999000999  # Bot 自身 ID，用于 should_skip_sender 判定
    bot.delete_message = AsyncMock()
    return bot


@pytest.fixture(autouse=True)
def _isolate_admin_ids(monkeypatch):
    """默认清空超级管理员列表，避免受全局配置影响"""
    from src.core.config import settings

    monkeypatch.setattr(settings, "admin_ids", [])


def make_message(
    *,
    chat_type: str = "supergroup",
    chat_id: int = -1001234567890,
    from_user_id: int | None = 100200300,
    sender_chat: MagicMock | None = None,
    new_chat_members: list | None = None,
    left_chat_member: MagicMock | None = None,
    message_id: int = 42,
) -> MagicMock:
    """构造 spec=Message 的 mock 事件，通过 isinstance 检查"""
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock()
    msg.chat.type = chat_type
    msg.chat.id = chat_id
    msg.from_user = None
    if from_user_id is not None:
        msg.from_user = MagicMock()
        msg.from_user.id = from_user_id
    msg.sender_chat = sender_chat
    msg.new_chat_members = new_chat_members
    msg.left_chat_member = left_chat_member
    msg.message_id = message_id
    return msg


def _patch_redis(mock_redis: MagicMock):
    """patch 中间件模块的 get_redis"""
    return patch(
        "src.bot.middlewares.verification_guard.get_redis",
        return_value=mock_redis,
    )


# ==================== 放行场景 ====================


@pytest.mark.asyncio
async def test_passthrough_non_message(middleware, mock_bot):
    """非 Message 事件直接放行"""
    handler = AsyncMock(return_value="handled")
    result = await middleware(handler, MagicMock(spec=object), {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_private_chat(middleware, mock_bot):
    """私聊消息放行"""
    msg = make_message(chat_type="private")
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_no_from_user(middleware, mock_bot):
    """无 from_user 的消息放行（频道消息、服务通知等）"""
    msg = make_message(from_user_id=None)
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_new_chat_members(middleware, mock_bot):
    """入群服务消息放行（由 ChatMemberUpdated 处理）"""
    msg = make_message(new_chat_members=[MagicMock()])
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_left_chat_member(middleware, mock_bot):
    """退群服务消息放行"""
    msg = make_message(left_chat_member=MagicMock())
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_sender_chat(middleware, mock_bot):
    """频道马甲 / 匿名管理员消息放行（交反频道逻辑）"""
    msg = make_message(sender_chat=MagicMock())
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_super_admin(middleware, mock_bot, monkeypatch):
    """超级管理员消息放行"""
    from src.core.config import settings

    admin_id = 100200300
    monkeypatch.setattr(settings, "admin_ids", [admin_id])
    msg = make_message(from_user_id=admin_id)
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_bot_self(middleware, mock_bot):
    """Bot 自身消息放行（should_skip_sender）"""
    msg = make_message(from_user_id=mock_bot.id)
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_telegram_service_account(middleware, mock_bot):
    """Telegram 系统服务账号（777000）放行"""
    msg = make_message(from_user_id=777000)
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_when_no_joining_marker(middleware, mock_bot):
    """无入群标记时放行下游"""
    msg = make_message()
    mock_redis = MagicMock()
    mock_redis.exists = AsyncMock(return_value=0)  # 标记不存在
    handler = AsyncMock(return_value="handled")

    with _patch_redis(mock_redis):
        result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()
    mock_bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_passthrough_on_redis_error(middleware, mock_bot):
    """Redis 查询异常时 fail-open 放行，不删消息"""
    msg = make_message()
    mock_redis = MagicMock()
    mock_redis.exists = AsyncMock(side_effect=RuntimeError("redis down"))
    handler = AsyncMock(return_value="handled")

    with _patch_redis(mock_redis):
        result = await middleware(handler, msg, {"bot": mock_bot})

    assert result == "handled"
    handler.assert_awaited_once()
    mock_bot.delete_message.assert_not_awaited()


# ==================== 命中场景 ====================


@pytest.mark.asyncio
async def test_delete_message_and_block_when_joining(middleware, mock_bot):
    """有入群标记时删除消息并阻断后续 handler"""
    msg = make_message(message_id=777)
    mock_redis = MagicMock()
    mock_redis.exists = AsyncMock(return_value=1)  # 标记存在
    handler = AsyncMock(return_value="handled")

    with _patch_redis(mock_redis):
        result = await middleware(handler, msg, {"bot": mock_bot})

    # 阻断：返回 None 且下游 handler 未被调用
    assert result is None
    handler.assert_not_awaited()

    # 删除消息用的是正确的 chat_id / message_id
    mock_bot.delete_message.assert_awaited_once()
    kwargs = mock_bot.delete_message.call_args.kwargs
    assert kwargs["chat_id"] == msg.chat.id
    assert kwargs["message_id"] == 777

    # 查的是正确的 joining key
    expected_key = RedisKeys.verification_joining(msg.chat.id, msg.from_user.id)
    mock_redis.exists.assert_awaited_once_with(expected_key)


@pytest.mark.asyncio
async def test_block_even_if_delete_fails(middleware, mock_bot):
    """删除失败仍阻断后续（消息可能已删，但不再进入反垃圾等流程）"""
    msg = make_message()
    mock_redis = MagicMock()
    mock_redis.exists = AsyncMock(return_value=1)
    mock_bot.delete_message = AsyncMock(side_effect=RuntimeError("forbidden"))
    handler = AsyncMock(return_value="handled")

    with _patch_redis(mock_redis):
        result = await middleware(handler, msg, {"bot": mock_bot})

    assert result is None
    handler.assert_not_awaited()
    mock_bot.delete_message.assert_awaited_once()


# ==================== 键名格式 ====================


def test_joining_key_format():
    """键名格式符合约定"""
    key = RedisKeys.verification_joining(-1001234567890, 100200300)
    assert key == "verification_joining:-1001234567890:100200300"


# ==================== 配置默认值 ====================


def test_default_joining_window_seconds():
    """入群短窗口默认时长为 3 秒"""
    from src.core.config import Settings

    # 直接读取字段定义的默认值，避免依赖会创建实例的 fixture
    assert Settings.model_fields["verification_joining_window_seconds"].default == 3
