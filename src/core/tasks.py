"""后台任务管理

asyncio 仅弱引用运行中的 Task：fire-and-forget 任务在长 sleep / await 期间可能被
GC 静默回收（无异常、无日志、功能无声消失）。此模块用模块级强引用集合兜底，
替代各调用点手写的 ``set + add + add_done_callback(discard)`` 三件套。
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from loguru import logger

# 模块级强引用：任务完成即移除
_background_tasks: set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    """移除强引用并消费/记录未处理异常（否则 asyncio 会报 'Task exception was never retrieved'）"""
    _background_tasks.discard(task)
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.error(f"后台任务异常结束: {task.get_coro()!r} -> {exc!r}")


def spawn_background_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """创建 fire-and-forget 后台任务并持有强引用直至完成

    调用方无需（也不应）自行保存 create_task 返回的引用。
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task
