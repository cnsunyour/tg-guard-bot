"""群组白名单中间件 - 仅在白名单群组中提供服务"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    Message,
    TelegramObject,
)
from loguru import logger

from src.repositories.group_repo import GroupRepository

if TYPE_CHECKING:
    from src.core.i18n import BoundLocalizer


class WhitelistMiddleware(BaseMiddleware):
    """群组白名单中间件

    只允许 Bot 在白名单群组中提供服务
    非白名单群组会自动退出

    在 message observer 上以 outer middleware 注册：无论消息类型是否有 handler
    都先过白名单，避免无 handler 的消息类型绕过后续闸门（见 main.py 注释）。
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """检查群组白名单"""
        localizer: BoundLocalizer = data["localizer"]

        # 获取 bot 实例
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            return await handler(event, data)

        # 获取群组 ID
        chat_id = None
        if isinstance(event, Message):
            if event.chat:
                chat_id = event.chat.id
                chat_type = event.chat.type
        elif isinstance(event, CallbackQuery):
            if event.message and event.message.chat:
                chat_id = event.message.chat.id
                chat_type = event.message.chat.type
        elif isinstance(event, (ChatMemberUpdated, ChatJoinRequest)):
            if event.chat:
                chat_id = event.chat.id
                chat_type = event.chat.type
        else:
            # 未知事件类型，放行
            return await handler(event, data)

        # 私聊不检查白名单
        if not chat_id or chat_type == ChatType.PRIVATE:
            return await handler(event, data)

        # 入群 / 退群 service message 放行：Bot 自身被加入非白名单群的退群动作由
        # events.on_bot_added_to_group（my_chat_member）负责；本中间件以 outer 挂在
        # message observer 上会同时收到对应的 new_chat_members 通知，若不放行会
        # 重复发提示 + 重复 leave_chat
        if isinstance(event, Message) and (event.new_chat_members or event.left_chat_member):
            return await handler(event, data)

        # 检查群组是否在白名单
        group = await GroupRepository.get_by_id(chat_id)

        if group and group.is_whitelisted:
            # 在白名单中，继续处理
            return await handler(event, data)

        # 不在白名单中
        group_name = ""
        if isinstance(event, Message):
            group_name = event.chat.title or str(chat_id)
        elif isinstance(event, CallbackQuery) and event.message:
            group_name = event.message.chat.title or str(chat_id)
        elif isinstance(event, (ChatMemberUpdated, ChatJoinRequest)):
            group_name = event.chat.title or str(chat_id)

        logger.warning(f"群组不在白名单中，准备退出: {group_name} (ID: {chat_id})")

        # 尝试发送提示消息（与 events 入群未授权复用同一 key，统一含 chat_id）
        try:
            if bot:
                await bot.send_message(
                    chat_id=chat_id,
                    text=localizer.t(
                        "common.group.unauthorized.message",
                        chat_id=chat_id,
                    ),
                )
        except Exception as e:
            logger.debug(f"发送退出提示消息失败: {e}")

        # 退出群组
        try:
            if bot:
                await bot.leave_chat(chat_id)
                logger.info(f"已退出非白名单群组: {group_name} (ID: {chat_id})")
        except Exception as e:
            logger.error(f"退出群组失败 {chat_id}: {e}")

        # 不继续处理
        return None
