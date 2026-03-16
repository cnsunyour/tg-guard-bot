"""CAS 黑名单检查中间件 - 检查消息发送者是否在 CAS 黑名单中"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject
from loguru import logger

from src.core.cache import PermissionCache
from src.core.config import settings
from src.core.utils import auto_delete_message
from src.repositories.audit_repo import AuditRepository
from src.services.cas_service import get_cas_service


class CASCheckMiddleware(BaseMiddleware):
    """CAS 黑名单检查中间件

    在所有消息处理之前检查发送者是否在 CAS 黑名单中。
    如果在黑名单中：删除消息 + 封禁用户 + 发送群内通知。

    仅处理群组消息，跳过私聊、超级管理员和群组管理员。
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # 仅处理 Message 类型
        if not isinstance(event, Message):
            return await handler(event, data)

        # 未启用直接放行
        if not settings.cas_enabled:
            return await handler(event, data)

        # 跳过私聊
        if event.chat.type == ChatType.PRIVATE:
            return await handler(event, data)

        # 跳过没有 from_user 的消息（频道消息等）
        if not event.from_user:
            return await handler(event, data)

        # 跳过入群/退群服务消息（由 ChatMemberUpdated 处理）
        if event.new_chat_members or event.left_chat_member:
            return await handler(event, data)

        # 跳过超级管理员
        if event.from_user.id in settings.admin_ids:
            return await handler(event, data)

        bot: Bot = data["bot"]

        # 跳过群组管理员
        if await PermissionCache.is_admin(bot, event.chat.id, event.from_user.id):
            return await handler(event, data)

        # 执行 CAS 检查
        cas_service = get_cas_service()
        cas_result = await cas_service.check_user(event.from_user.id)

        if not cas_result.is_banned:
            return await handler(event, data)

        # === 黑名单用户处理 ===
        chat_id = event.chat.id
        user_id = event.from_user.id

        # 删除消息
        try:
            await event.delete()
        except Exception as e:
            logger.debug(f"CAS 删除消息失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        # 封禁用户
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            logger.warning(f"CAS 封禁用户失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        # 记录审计日志
        try:
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=bot.id,
                action="cas_ban_on_message",
                target_user_id=user_id,
                details={"offenses": cas_result.offenses},
            )
        except Exception as e:
            logger.warning(f"CAS 审计日志写入失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        # 发送群内通知
        try:
            notify_msg = await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚫 <a href=\"tg://user?id={user_id}\">{user_id}</a> 在 CAS 黑名单中，"
                    f"已被自动封禁（违规 {cas_result.offenses} 次）。"
                ),
                parse_mode="HTML",
            )
            await auto_delete_message(notify_msg, delay=30)
        except Exception as e:
            logger.debug(f"CAS 发送封禁通知失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        logger.info(
            f"CAS 中间件拦截黑名单用户 [群组:{chat_id}] [用户:{user_id}] "
            f"[违规次数:{cas_result.offenses}] [缓存:{cas_result.cached}]"
        )

        return None  # 阻止事件继续传播
