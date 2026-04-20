"""宵禁模式中间件"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from loguru import logger

from src.core.cache import PermissionCache
from src.core.config import settings
from src.repositories.group_repo import GroupRepository
from src.services.activity import ActivityService
from src.services.curfew import CurfewService


class CurfewMiddleware(BaseMiddleware):
    """宵禁模式中间件

    在宵禁期间根据用户活跃度限制消息发送
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # 仅处理 Message 事件
        if not isinstance(event, Message):
            return await handler(event, data)

        message = event

        # 跳过私聊
        if message.chat.type == "private":
            return await handler(event, data)

        # 跳过无发送者的消息
        if not message.from_user:
            return await handler(event, data)

        # 跳过管理员
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            return await handler(event, data)

        if message.from_user.id in settings.admin_ids:
            return await handler(event, data)

        if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
            return await handler(event, data)

        # 获取群组配置
        group = await GroupRepository.get(message.chat.id)
        if not group or not group.curfew_enabled:
            return await handler(event, data)

        # 检查是否在宵禁期
        if not CurfewService.is_in_curfew(group):
            return await handler(event, data)

        # 获取用户活跃度
        activity = await ActivityService.get_activity(message.chat.id, message.from_user.id)

        # 判断是否为纯文本消息
        is_text = bool(
            message.text
            and not message.photo
            and not message.sticker
            and not message.video
            and not message.animation
            and not message.voice
            and not message.video_note
            and not message.document
            and not message.audio
        )

        # 检查是否允许
        allowed, reason = await CurfewService.check_message_allowed(group, activity, is_text)

        if not allowed:
            # 删除消息
            try:
                await message.delete()
                logger.info(
                    f"宵禁限制：已删除消息 [群组:{message.chat.id}] "
                    f"[用户:{message.from_user.id}] [活跃度:{activity}] "
                    f"[原因:{reason}]"
                )
            except Exception as e:
                logger.error(f"删除宵禁限制消息失败: {e}")

            return None  # 停止处理

        # 允许消息
        return await handler(event, data)
