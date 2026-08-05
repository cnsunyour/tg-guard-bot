"""速率限制中间件 - 防止 DoS 攻击"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from loguru import logger

from src.core.redis import get_redis

if TYPE_CHECKING:
    from src.core.i18n import BoundLocalizer


class ThrottleMiddleware(BaseMiddleware):
    """速率限制中间件（基于 Redis）"""

    def __init__(
        self,
        rate_limit: int = 3,  # 允许的请求数
        time_window: int = 1,  # 时间窗口（秒）
        prefix: str = "throttle",
    ):
        """初始化速率限制中间件

        Args:
            rate_limit: 时间窗口内允许的最大请求数
            time_window: 时间窗口（秒）
            prefix: Redis 键前缀
        """
        super().__init__()
        self.rate_limit = rate_limit
        self.time_window = time_window
        self.prefix = prefix

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """处理速率限制"""
        localizer: BoundLocalizer = data["localizer"]

        # 获取用户 ID
        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None
            chat_id = event.chat.id if event.chat else None
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else None
            chat_id = event.message.chat.id if event.message and event.message.chat else None
        else:
            # 未知事件类型，放行
            return await handler(event, data)

        if not user_id:
            # 没有用户 ID，放行
            return await handler(event, data)

        # 构建 Redis 键
        key = f"{self.prefix}:{user_id}"
        if chat_id:
            key = f"{self.prefix}:{chat_id}:{user_id}"

        redis = get_redis()

        try:
            # 获取当前计数
            count = await redis.get(key)

            if count is None:
                # 首次请求，设置计数为 1
                await redis.setex(key, self.time_window, "1")
                return await handler(event, data)

            count = int(count)

            if count >= self.rate_limit:
                # 超过速率限制
                logger.warning(
                    f"速率限制触发 [用户:{user_id}] [群组:{chat_id}] 计数:{count}/{self.rate_limit}"
                )

                # ✅ P0-3: 发送警告消息
                # Message 类型不支持 show_alert 参数；callback toast 另用 key（长度限制惯例）
                if isinstance(event, Message):
                    await event.answer(localizer.t("middleware.throttle.rate_limited.message"))
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        localizer.t("middleware.throttle.rate_limited.toast"),
                        show_alert=True,
                    )

                # 不继续处理
                return None

            # 增加计数
            await redis.incr(key)

            # 继续处理
            return await handler(event, data)

        except Exception as e:
            logger.error(f"速率限制检查失败: {e}")
            # 出错时放行，避免影响正常功能
            return await handler(event, data)
