"""自动删除消息中间件"""

from typing import Callable, Dict, Any, Awaitable
import asyncio

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from loguru import logger


class AutoDeleteMiddleware(BaseMiddleware):
    """自动删除群组中的命令消息和响应消息

    功能：
    1. 管理员发送命令后，立即删除命令消息
    2. Bot 的响应消息在 30 秒后自动删除

    注意：
    - 仅在群组中生效
    - 仅处理命令消息（以 / 开头）
    - 删除失败会静默处理
    """

    def __init__(self, response_delay: int = 30):
        """初始化中间件

        Args:
            response_delay: 响应消息的删除延迟（秒），默认30秒
        """
        self.response_delay = response_delay
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        """中间件处理函数"""

        # 只处理群组消息
        if event.chat.type == "private":
            return await handler(event, data)

        # 只处理命令消息（以 / 开头）
        if not event.text or not event.text.startswith("/"):
            return await handler(event, data)

        # 执行命令处理器
        result = await handler(event, data)

        # 处理完成后，删除原始命令消息
        try:
            await event.delete()
            logger.debug(
                f"已删除命令消息 [群组:{event.chat.id}] "
                f"[用户:{event.from_user.id}] [命令:{event.text.split()[0]}]"
            )
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")

        # 如果处理器返回了消息对象，设置自动删除
        if isinstance(result, Message):
            asyncio.create_task(self._auto_delete_response(result))

        return result

    async def _auto_delete_response(self, message: Message) -> None:
        """自动删除响应消息（延迟删除）"""
        try:
            await asyncio.sleep(self.response_delay)
            await message.delete()
            logger.debug(
                f"已自动删除响应消息 [群组:{message.chat.id}] [消息ID:{message.message_id}]"
            )
        except Exception as e:
            logger.debug(
                f"自动删除响应消息失败 [群组:{message.chat.id}] "
                f"[消息ID:{message.message_id}]: {e}"
            )
