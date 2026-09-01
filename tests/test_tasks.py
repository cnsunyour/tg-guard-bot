"""后台任务强引用管理测试"""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.core import tasks
from src.core.tasks import spawn_background_task

pytestmark = pytest.mark.unit


async def test_holds_strong_reference_until_done():
    """任务完成前保留强引用，完成后移除"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _worker():
        started.set()
        await release.wait()

    task = spawn_background_task(_worker())
    await started.wait()
    assert task in tasks._background_tasks

    release.set()
    await task
    await asyncio.sleep(0)  # 让 done_callback 执行
    assert task not in tasks._background_tasks


async def test_consumes_and_logs_task_exception(mocker):
    """异常任务：异常被消费（无 'never retrieved'）并记录日志"""
    logged = MagicMock()
    mocker.patch.object(tasks.logger, "error", side_effect=logged)

    async def _boom():
        raise RuntimeError("boom")

    task = spawn_background_task(_boom())
    with pytest.raises(RuntimeError, match="boom"):
        await task
    await asyncio.sleep(0)  # 让 done_callback 执行

    assert task not in tasks._background_tasks
    assert logged.call_count == 1
    # 异常已被 callback 消费：再次 exception() 不触发 never-retrieved 警告
    assert task.exception() is not None


async def test_cancelled_task_not_logged_as_error(mocker):
    """被取消的任务不算异常，不打错误日志"""
    logged = MagicMock()
    mocker.patch.object(tasks.logger, "error", side_effect=logged)

    async def _sleeper():
        await asyncio.sleep(60)

    task = spawn_background_task(_sleeper())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert task not in tasks._background_tasks
    logged.assert_not_called()
