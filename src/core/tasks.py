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


async def cancel_all_background_tasks() -> int:
    """取消所有后台任务并等待退出（进程关闭时调用）

    后台任务（如验证 timeout 协程）可能在长 sleep 后唤醒并访问
    Redis/DB/Bot session——若不取消，它们会在这些依赖陆续关闭后唤醒，
    触发连接重建与竞态。被取消任务的持久化状态（如 Redis deadline）
    保留，进程重启后由启动恢复扫描重新派发。

    已知权衡：若取消恰逢任务已 claim 状态并正在执行处罚副作用（秒级
    窗口），该次处罚随取消丢失且 claim 已消费主键、重启不会重派——相比
    不取消（任务会在依赖关闭后必然失败，逃逸窗口为 deadline 剩余全时长），
    此方案的逃逸窗口缩小到副作用执行瞬间，严格更优。
    """
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)
