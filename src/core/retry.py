"""网络请求重试装饰器

用于自动重试因网络临时错误失败的操作
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from loguru import logger

P = ParamSpec("P")
T = TypeVar("T")


def retry_on_network_error(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 10.0,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """网络错误自动重试装饰器

    Args:
        max_retries: 最大重试次数（默认 3 次）
        initial_delay: 初始延迟时间（秒，默认 1 秒）
        backoff_factor: 退避因子（默认 2.0，指数退避）
        max_delay: 最大延迟时间（秒，默认 10 秒）

    支持的异常类型：
        - TelegramNetworkError: 网络连接错误
        - TelegramServerError: Telegram 服务器错误（5xx）
        - TelegramRetryAfter: 速率限制（会等待指定时间）
        - ConnectionError: 通用连接错误
        - TimeoutError: 超时错误

    使用示例：
        @retry_on_network_error(max_retries=3)
        async def check_admin(bot, chat_id, user_id):
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status in ["creator", "administrator"]
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exception: BaseException | None = None
            delay = initial_delay

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)

                except TelegramRetryAfter as e:
                    # Telegram 速率限制，等待指定时间
                    retry_after = e.retry_after
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 触发速率限制，等待 {retry_after} 秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries + 1})"
                        )
                        await asyncio.sleep(retry_after)
                        last_exception = e
                        continue
                    else:
                        logger.error(f"{func.__name__} 达到最大重试次数，速率限制未解除")
                        raise

                except (
                    TelegramNetworkError,
                    TelegramServerError,
                    ConnectionError,
                    TimeoutError,
                ) as e:
                    # 网络临时错误，指数退避重试
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 网络错误: {type(e).__name__}: {e}, "
                            f"等待 {delay:.1f} 秒后重试 (尝试 {attempt + 1}/{max_retries + 1})"
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                        last_exception = e
                        continue
                    else:
                        logger.error(
                            f"{func.__name__} 达到最大重试次数，最后错误: {type(e).__name__}: {e}"
                        )
                        raise

                except Exception as e:
                    # 其他异常不重试，直接抛出
                    logger.error(f"{func.__name__} 发生非网络错误: {type(e).__name__}: {e}")
                    raise

            # 理论上不会到达这里，但为了类型检查
            if last_exception:
                raise last_exception
            raise RuntimeError(f"{func.__name__} 重试逻辑异常")

        return wrapper

    return decorator
