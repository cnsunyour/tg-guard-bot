"""群组白名单中间件 - 仅在白名单群组中提供服务"""

from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated
from aiogram.enums import ChatType
from loguru import logger

from src.repositories.group_repo import GroupRepository
from src.core.database import get_session


class WhitelistMiddleware(BaseMiddleware):
    """群组白名单中间件

    只允许 Bot 在白名单群组中提供服务
    非白名单群组会自动退出
    """

    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery | ChatMemberUpdated, Dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery | ChatMemberUpdated,
        data: Dict[str, Any],
    ) -> Any:
        """检查群组白名单"""

        # 获取 bot 实例
        bot: Bot = data.get("bot")

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
        elif isinstance(event, ChatMemberUpdated):
            if event.chat:
                chat_id = event.chat.id
                chat_type = event.chat.type
        else:
            # 未知事件类型，放行
            return await handler(event, data)

        # 私聊不检查白名单
        if not chat_id or chat_type == ChatType.PRIVATE:
            return await handler(event, data)

        # 检查群组是否在白名单
        async with get_session() as session:
            group_repo = GroupRepository(session)
            group = await group_repo.get_by_id(chat_id)

            if group and group.is_whitelisted:
                # 在白名单中，继续处理
                return await handler(event, data)

            # 不在白名单中
            group_name = ""
            if isinstance(event, Message):
                group_name = event.chat.title or str(chat_id)
            elif isinstance(event, CallbackQuery) and event.message:
                group_name = event.message.chat.title or str(chat_id)
            elif isinstance(event, ChatMemberUpdated):
                group_name = event.chat.title or str(chat_id)

            logger.warning(
                f"群组不在白名单中，准备退出: {group_name} (ID: {chat_id})"
            )

            # 尝试发送提示消息
            try:
                if bot:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ 此群组未在授权列表中，Bot 将自动退出。\n"
                             "如需使用，请联系 Bot 管理员添加群组白名单。"
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
