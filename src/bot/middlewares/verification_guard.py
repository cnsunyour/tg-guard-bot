"""入群短窗口消息防护中间件

在 on_user_join 写入的 verification_joining 标记存续期内，删除该新成员在群里发送的
消息，用于拦截 restrict_chat_member 生效前的抢发（正常用户入群后极少立即发言）。
命中只删除消息，不做封禁等其它处理；不依赖验证状态，对所有入群者一视同仁。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject
from loguru import logger

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis
from src.core.utils import should_skip_sender


class VerificationGuardMiddleware(BaseMiddleware):
    """删除新成员入群短窗口内发送的群消息，并阻断后续处理。

    设计要点：
    - 仅对新发的群组 Message 生效（不处理 edited_message / callback_query）。
    - 跳过私聊、服务消息、频道马甲、匿名管理员、超级管理员、系统账号与 Bot 自身。
    - Redis 查询失败时 fail-open 放行，绝不因基础设施抖动误删普通用户消息。
    - 命中即删除消息并 return None，不进入宵禁 / 自动删除 / CAS / 反垃圾等后续逻辑。
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # 仅处理新发的 Message
        if not isinstance(event, Message):
            return await handler(event, data)

        # 仅处理群组（普通群 / 超级群），放行私聊与频道
        if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return await handler(event, data)

        # 跳过没有 from_user 的消息（频道消息、服务通知等）
        if not event.from_user:
            return await handler(event, data)

        # 跳过入群 / 退群服务消息（由 ChatMemberUpdated 处理）
        if event.new_chat_members or event.left_chat_member:
            return await handler(event, data)

        # 跳过频道马甲与匿名管理员消息（无法可靠映射到真实用户，交反频道逻辑）
        if event.sender_chat is not None:
            return await handler(event, data)

        # 跳过超级管理员
        if event.from_user.id in settings.admin_ids:
            return await handler(event, data)

        bot: Bot = data["bot"]

        # 跳过 Telegram 系统服务账号与 Bot 自身
        if should_skip_sender(event.from_user.id, bot.id):
            return await handler(event, data)

        chat_id = event.chat.id
        user_id = event.from_user.id
        message_id = event.message_id

        # 查入群短窗口标记（fail-open）
        try:
            joining = await get_redis().exists(RedisKeys.verification_joining(chat_id, user_id)) > 0
        except Exception as e:
            # WARNING 而非 ERROR：Redis 抖动时每条群消息都会命中此分支，
            # ERROR 会在 Sentry（event_level=40）造成事件洪泛；fail-open 可降级，仅记日志。
            logger.warning(f"查询入群短窗口标记失败，放行 [群组:{chat_id}] [用户:{user_id}]: {e}")
            return await handler(event, data)

        if not joining:
            return await handler(event, data)

        # 命中：只删消息，不做其它处理
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info(
                f"拦截入群短窗口抢发消息 [群组:{chat_id}] [用户:{user_id}] " f"[消息:{message_id}]"
            )
        except Exception as e:
            logger.warning(
                f"删除入群短窗口抢发消息失败（消息可能已删或权限不足）"
                f" [群组:{chat_id}] [用户:{user_id}] [消息:{message_id}]: {e}"
            )

        return None  # 阻断后续中间件与 handler
