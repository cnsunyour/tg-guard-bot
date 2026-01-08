"""Telegram API 速率限制处理中间件

此中间件在 client session 层面处理 TelegramRetryAfter (429) 异常，
自动等待并重试请求，对业务代码透明。
"""

import asyncio
from typing import TypeVar

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import Response, TelegramMethod
from loguru import logger

TelegramType = TypeVar("TelegramType")


class RetryAfterMiddleware(BaseRequestMiddleware):
    """处理 Telegram API 速率限制（429 错误）的中间件

    当收到 TelegramRetryAfter 异常时，自动等待指定时间后重试请求。
    支持配置最大重试次数，避免无限循环。

    特性：
    - 自动处理 429 错误，对业务代码透明
    - 尊重 Telegram API 返回的 retry_after 时间
    - 记录详细日志，便于监控和调试
    - 支持配置最大重试次数
    """

    def __init__(self, max_retries: int = 3) -> None:
        """初始化中间件

        Args:
            max_retries: 最大重试次数，避免无限循环
        """
        self.max_retries = max_retries

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        """执行请求，捕获并处理速率限制异常

        Args:
            make_request: 下一个中间件或实际请求函数
            bot: Bot 实例
            method: Telegram API 方法对象

        Returns:
            API 响应对象

        Raises:
            TelegramRetryAfter: 达到最大重试次数后仍然失败
        """
        retry_count = 0

        while True:
            try:
                # 执行请求
                response = await make_request(bot, method)
                return response

            except TelegramRetryAfter as e:
                retry_count += 1

                # 如果达到最大重试次数，抛出异常
                if retry_count > self.max_retries:
                    logger.error(
                        f"❌ Telegram API 速率限制重试失败，已达到最大重试次数 "
                        f"[方法:{method.__class__.__name__}] "
                        f"[重试次数:{retry_count}] "
                        f"[等待时间:{e.retry_after}秒]"
                    )
                    raise

                # 记录日志并等待
                logger.warning(
                    f"⏳ Telegram API 速率限制触发，自动重试中 "
                    f"[方法:{method.__class__.__name__}] "
                    f"[第{retry_count}次重试] "
                    f"[等待时间:{e.retry_after}秒]"
                )

                # 等待指定时间后重试
                await asyncio.sleep(e.retry_after)

                logger.debug(
                    f"🔄 重试 Telegram API 请求 "
                    f"[方法:{method.__class__.__name__}] "
                    f"[第{retry_count}次重试]"
                )
