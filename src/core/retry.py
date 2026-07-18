"""网络请求重试工具

提供两种形式的网络临时错误重试：

- ``retry_on_network_error``：装饰器形式，用于包装独立的异步函数。
- ``retry_async_call``：调用时形式，用于对已有协程方法（如 bot API 调用）
  做一次性重试，无需定义新的被装饰函数。
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

# 可重试的网络临时错误类型（装饰器与 retry_async_call 共享）
# 注意：TelegramRetryAfter 因需读取 e.retry_after 单独处理，不在此元组中
_RETRYABLE_NETWORK_ERRORS = (
    TelegramNetworkError,
    TelegramServerError,
    ConnectionError,
    TimeoutError,
)


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

                except _RETRYABLE_NETWORK_ERRORS as e:
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


async def retry_async_call[T](
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 10.0,
) -> T:
    """调用时形式的网络错误重试。

    与 :func:`retry_on_network_error` 装饰器语义一致：仅重试网络临时错误
    （含 429 速率限制），非网络错误立即抛出，重试耗尽抛出最后一次异常。

    适合对已有协程方法（如 ``bot.approve_chat_join_request``）做一次性重试，
    无需定义新的被装饰函数。每次重试都重新调用 ``coro_factory``，避免复用
    已 await 过的协程对象。

    与装饰器的差异：非网络错误直接传播，**不在此处记日志**（由调用方统一记录），
    避免与调用方的异常处理产生重复 error 日志。

    Args:
        coro_factory: 返回待执行 coroutine 的零参工厂（每次重试重新调用）。
        max_retries: 最大重试次数，不含首次尝试（默认 3，即最多尝试 4 次）。
        initial_delay: 首次重试前等待秒数。
        backoff_factor: 退避因子（指数增长）。
        max_delay: 单次等待上限（秒）。

    Returns:
        工厂协程的返回值。

    Raises:
        重试耗尽后抛出最后一次捕获的可重试异常；非网络错误立即原样抛出。
    """
    delay = initial_delay
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            if attempt == max_retries:
                raise
            logger.warning(
                f"触发速率限制，等待 {e.retry_after} 秒后重试 "
                f"(尝试 {attempt + 1}/{max_retries + 1})"
            )
            await asyncio.sleep(e.retry_after)
        except _RETRYABLE_NETWORK_ERRORS as e:
            if attempt == max_retries:
                raise
            logger.warning(
                f"网络错误: {type(e).__name__}: {e}，"
                f"等待 {delay:.1f} 秒后重试 (尝试 {attempt + 1}/{max_retries + 1})"
            )
            await asyncio.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)
    # 理论不可达：循环内最后一次 attempt 必然 return 或 raise
    raise RuntimeError("retry_async_call 重试逻辑异常")
