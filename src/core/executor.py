"""线程池执行器 - 处理 CPU 密集型操作

✅ P1-11: 将 ML 推理、OCR 等 CPU 密集型操作移至线程池，避免阻塞事件循环
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from loguru import logger

from src.core.config import settings

T = TypeVar("T")

# 全局线程池实例
_executor: ThreadPoolExecutor | None = None


def get_executor() -> ThreadPoolExecutor:
    """获取全局线程池实例

    Returns:
        线程池执行器
    """
    global _executor
    if _executor is None:
        # 根据 CPU 核心数设置线程数
        # 对于 CPU 密集型任务，通常设置为 CPU 核心数
        max_workers = getattr(settings, "cpu_executor_workers", None) or 2
        _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cpu_worker_")
        logger.info(f"线程池已初始化: max_workers={max_workers}")
    return _executor


async def run_in_executor[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在线程池中运行同步 CPU 密集型函数

    Args:
        func: 要执行的同步函数
        *args: 位置参数
        **kwargs: 关键字参数

    Returns:
        函数执行结果

    Example:
        result = await run_in_executor(classifier.predict, text)
    """
    executor = get_executor()
    loop = asyncio.get_event_loop()

    # 使用 functools.partial 处理关键字参数
    if kwargs:
        from functools import partial

        func = partial(func, **kwargs)

    try:
        result = await loop.run_in_executor(executor, func, *args)
        return result
    except Exception as e:
        logger.error(f"线程池执行失败 [函数:{func.__name__}]: {e}")
        raise


def shutdown_executor(wait: bool = True) -> None:
    """关闭线程池

    Args:
        wait: 是否等待所有任务完成
    """
    global _executor
    if _executor is not None:
        logger.info("正在关闭线程池...")
        _executor.shutdown(wait=wait)
        _executor = None
        logger.info("线程池已关闭")
