"""CAS 黑名单检查中间件 - 检查消息发送者是否在 CAS 黑名单中或为异常用户"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject, User
from loguru import logger

from src.core.cache import PermissionCache
from src.core.config import settings
from src.core.i18n import BoundLocalizer
from src.core.utils import auto_delete_message, masked_mention_html, should_skip_sender
from src.repositories.audit_repo import AuditRepository
from src.services.cas_service import get_cas_service
from src.services.user_status_service import get_user_status_service


class CASCheckMiddleware(BaseMiddleware):
    """CAS 黑名单检查中间件

    在所有消息处理之前检查发送者是否在 CAS 黑名单中或为异常用户。
    如果在黑名单中或为异常用户：删除消息 + 封禁用户 + 发送群内通知。

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
        if not settings.cas_enabled and not settings.user_status_check_enabled:
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
        localizer: BoundLocalizer = data["localizer"]

        # 跳过 Telegram 系统服务账号（777000 等）和 Bot 自身
        # 避免对关联频道同步转发等服务消息发起无谓的 CAS / 状态查询
        if should_skip_sender(event.from_user.id, bot.id):
            return await handler(event, data)

        # 跳过群组管理员
        if await PermissionCache.is_admin(bot, event.chat.id, event.from_user.id):
            return await handler(event, data)

        chat_id = event.chat.id
        user_id = event.from_user.id
        message_id = event.message_id

        # 1. 执行 CAS 检查
        if settings.cas_enabled:
            cas_service = get_cas_service()
            cas_result = await cas_service.check_user(user_id)

            if cas_result.is_banned:
                await self._handle_problematic_user(
                    bot=bot,
                    localizer=localizer,
                    chat_id=chat_id,
                    user_id=user_id,
                    user=event.from_user,
                    reason="cas_blacklist",
                    details={"offenses": cas_result.offenses},
                    cached=cas_result.cached,
                    message_id=message_id,
                )
                return None  # 阻止事件继续传播

        # 2. 执行用户状态检查
        if settings.user_status_check_enabled:
            status_service = get_user_status_service()
            status_result = await status_service.check_user(user_id, chat_id)

            if status_result.is_problematic:
                await self._handle_problematic_user(
                    bot=bot,
                    localizer=localizer,
                    chat_id=chat_id,
                    user_id=user_id,
                    user=event.from_user,
                    reason=f"user_status_{status_result.reason}",
                    details={"status": status_result.reason},
                    cached=status_result.cached,
                    message_id=message_id,
                )
                return None  # 阻止事件继续传播

        return await handler(event, data)

    async def _handle_problematic_user(
        self,
        bot: Bot,
        localizer: BoundLocalizer,
        chat_id: int,
        user_id: int,
        user: User,
        reason: str,
        details: dict[str, Any],
        cached: bool,
        message_id: int | None = None,
    ) -> None:
        """处理异常用户（统一处理逻辑）

        Args:
            bot: Bot 实例
            localizer: 当前 Update 绑定的本地化器
            chat_id: 群组 ID
            user_id: 用户 ID
            user: 用户对象（用于生成脱敏的群内通知）
            reason: 原因（cas_blacklist/user_status_restricted/user_status_scam 等）
            details: 详细信息
            cached: 是否来自缓存
            message_id: 消息 ID（可选，用于删除消息）
        """
        # 删除消息
        if message_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                logger.debug(f"删除消息失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        # 封禁用户
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            logger.warning(f"封禁用户失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        # 记录审计日志
        try:
            action = f"ban_on_message_{reason}"
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=bot.id,
                action=action,
                target_user_id=user_id,
                details=details,
            )
        except Exception as e:
            logger.warning(f"审计日志写入失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        # 发送群内通知
        try:
            notify_text = self._get_notification_text(localizer, user, reason, details)
            notify_msg = await bot.send_message(
                chat_id=chat_id,
                text=notify_text,
                parse_mode="HTML",
            )
            await auto_delete_message(notify_msg, delay=30)
        except Exception as e:
            logger.debug(f"发送封禁通知失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

        logger.info(
            f"拦截异常用户 [群组:{chat_id}] [用户:{user_id}] "
            f"[原因:{reason}] [详情:{details}] [缓存:{cached}]"
        )

    def _get_notification_text(
        self,
        localizer: BoundLocalizer,
        user: User,
        reason: str,
        details: dict[str, Any],
    ) -> str:
        """生成通知文本

        复用 verification.join.* 的 CAS/状态封禁文案（与入群验证通知一致）。
        user 经 masked_mention_html 脱敏为安全 HTML 片段，作 {user} 插入不二次转义。

        Args:
            localizer: 当前 Update 绑定的本地化器
            user: 用户对象（显示名将脱敏，防止 spammer 借用户名投广告）
            reason: 原因（cas_blacklist / user_status_*）
            details: 详细信息（user_status 携带 status）

        Returns:
            通知文本
        """
        user_link = masked_mention_html(user)

        if reason == "cas_blacklist":
            return localizer.t("verification.join.cas_ban.notify", user=user_link)

        # user_status_* 与未知 reason 统一走 status_ban.notify（不展示裸 reason）
        status = (
            details.get("status", "unknown") if reason.startswith("user_status_") else "unknown"
        )
        if status not in ("restricted", "scam", "fake", "deleted"):
            status = "unknown"
        status_text = localizer.t(f"verification.join.status_ban.{status}.label")
        return localizer.t(
            "verification.join.status_ban.notify",
            user=user_link,
            status=status_text,
        )
