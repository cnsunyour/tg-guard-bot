"""启动时恢复验证 timeout 的 handler 测试

覆盖 ``resume_pending_verification_timeouts`` 的核心契约：
- 扫描 ``verification_deadline:*``，按 flow 分派对应 timeout handler 并透传
  session_id；timeout 按群配置取值（缺失时回退全局默认）
- 坏键名/坏 deadline 值/flow 缺失或非法一律跳过，不派发任务
- 处罚时机仍由 Redis claim 状态机决定，本函数只负责派发
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import main
from src.bot.handlers import verification as handler
from src.core.redis import RedisKeys

pytestmark = pytest.mark.unit

CHAT_ID = -1001234567890
JOIN_USER_ID = 42
JOIN_REQUEST_USER_ID = 43


def _patch_redis(mocker, keys, values):
    """构造只实现 scan_iter/get 的 Redis mock。"""
    redis = MagicMock()

    async def _scan_iter(**_kwargs):
        for key in keys:
            yield key

    redis.scan_iter = MagicMock(side_effect=_scan_iter)
    redis.get = AsyncMock(side_effect=lambda key: values.get(key))
    mocker.patch.object(handler, "get_redis", return_value=redis)
    return redis


def _patch_group_repo(mocker, group_config):
    """构造返回固定群配置（或 None）的 GroupRepository mock。"""
    repo_cls = mocker.patch.object(handler, "GroupRepository")
    repo_cls.return_value.get = AsyncMock(return_value=group_config)
    return repo_cls.return_value


def _patch_task_runner(mocker):
    """收集 create_task 创建的任务，供测试 gather 等待完成。"""
    created = []
    real_create_task = asyncio.create_task

    def _create_task(coro):
        task = real_create_task(coro)
        created.append(task)
        return task

    mocker.patch.object(handler.asyncio, "create_task", side_effect=_create_task)
    return created


async def test_resume_dispatches_join_and_join_request_with_group_timeout(mocker) -> None:
    """join 与 join_request 分派到各自 handler，timeout 按群配置透传。"""
    join_timeout = mocker.patch.object(handler, "handle_verification_timeout", new=AsyncMock())
    join_request_timeout = mocker.patch.object(
        handler, "handle_join_request_timeout", new=AsyncMock()
    )

    join_key = RedisKeys.verification_deadline(CHAT_ID, JOIN_USER_ID)
    join_request_key = RedisKeys.verification_deadline(CHAT_ID, JOIN_REQUEST_USER_ID)
    values = {
        join_key: "session-join:9999999999999",
        join_request_key: "session-jr:9999999999999",
        RedisKeys.verification_type(CHAT_ID, JOIN_USER_ID): "join",
        RedisKeys.verification_type(CHAT_ID, JOIN_REQUEST_USER_ID): "join_request",
    }
    redis = _patch_redis(mocker, [join_key, join_request_key], values)
    _patch_group_repo(mocker, SimpleNamespace(verification_timeout=77))
    created = _patch_task_runner(mocker)
    bot = MagicMock()

    resumed = await handler.resume_pending_verification_timeouts(bot)
    await asyncio.gather(*created)

    assert resumed == 2
    redis.scan_iter.assert_called_once_with(match="verification_deadline:*", count=100)
    join_timeout.assert_awaited_once_with(
        bot, CHAT_ID, JOIN_USER_ID, session_id="session-join", timeout=77
    )
    join_request_timeout.assert_awaited_once_with(
        bot, CHAT_ID, JOIN_REQUEST_USER_ID, session_id="session-jr", timeout=77
    )


async def test_resume_falls_back_to_global_timeout_without_group_config(mocker) -> None:
    """群配置缺失时 timeout 回退全局默认值。"""
    join_timeout = mocker.patch.object(handler, "handle_verification_timeout", new=AsyncMock())

    join_key = RedisKeys.verification_deadline(CHAT_ID, JOIN_USER_ID)
    values = {
        join_key: "session-join:9999999999999",
        RedisKeys.verification_type(CHAT_ID, JOIN_USER_ID): "join",
    }
    _patch_redis(mocker, [join_key], values)
    _patch_group_repo(mocker, None)
    created = _patch_task_runner(mocker)
    bot = MagicMock()

    resumed = await handler.resume_pending_verification_timeouts(bot)
    await asyncio.gather(*created)

    assert resumed == 1
    join_timeout.assert_awaited_once_with(
        bot,
        CHAT_ID,
        JOIN_USER_ID,
        session_id="session-join",
        timeout=handler.settings.verification_timeout,
    )


async def test_resume_skips_malformed_keys_deadlines_and_flows(mocker) -> None:
    """坏键名、坏 deadline 值、flow 缺失或非法均跳过且不派发任务。"""
    join_timeout = mocker.patch.object(handler, "handle_verification_timeout", new=AsyncMock())
    join_request_timeout = mocker.patch.object(
        handler, "handle_join_request_timeout", new=AsyncMock()
    )

    missing_type_key = RedisKeys.verification_deadline(CHAT_ID, 44)
    invalid_type_key = RedisKeys.verification_deadline(CHAT_ID, 45)
    invalid_deadline_key = RedisKeys.verification_deadline(CHAT_ID, 46)
    colon_session_key = RedisKeys.verification_deadline(CHAT_ID, 47)
    vanished_key = RedisKeys.verification_deadline(CHAT_ID, 48)

    keys = [
        "verification_deadline:not-an-int:42",
        "verification_deadline:-100:42:extra",
        missing_type_key,
        invalid_type_key,
        invalid_deadline_key,
        colon_session_key,
        vanished_key,
    ]
    values = {
        missing_type_key: "session-a:9999999999999",
        invalid_type_key: "session-b:9999999999999",
        RedisKeys.verification_type(CHAT_ID, 45): "unknown",
        invalid_deadline_key: "no-deadline-here",
        colon_session_key: "bad:session:9999999999999",
        RedisKeys.verification_type(CHAT_ID, 47): "join",
        vanished_key: None,
        RedisKeys.verification_type(CHAT_ID, 48): "join",
    }
    _patch_redis(mocker, keys, values)
    created = _patch_task_runner(mocker)

    resumed = await handler.resume_pending_verification_timeouts(MagicMock())

    assert resumed == 0
    assert created == []
    join_timeout.assert_not_awaited()
    join_request_timeout.assert_not_awaited()


async def test_resume_deduplicates_scan_repeats(mocker) -> None:
    """SCAN 重复返回同一键时只派发一次。"""
    join_timeout = mocker.patch.object(handler, "handle_verification_timeout", new=AsyncMock())

    join_key = RedisKeys.verification_deadline(CHAT_ID, JOIN_USER_ID)
    values = {
        join_key: "session-join:9999999999999",
        RedisKeys.verification_type(CHAT_ID, JOIN_USER_ID): "join",
    }
    _patch_redis(mocker, [join_key, join_key], values)
    _patch_group_repo(mocker, None)
    created = _patch_task_runner(mocker)

    resumed = await handler.resume_pending_verification_timeouts(MagicMock())
    await asyncio.gather(*created)

    assert resumed == 1
    join_timeout.assert_awaited_once()


def _patch_on_startup_dependencies(mocker, resume_mock) -> None:
    """mock 掉 on_startup 的全部启动依赖（DB/Telethon/健康/命令/宵禁/i18n）。"""
    mocker.patch.object(handler, "resume_pending_verification_timeouts", new=resume_mock)
    from src import main as main_module

    mocker.patch.object(main_module, "init_db", new=AsyncMock())
    mocker.patch.object(main_module, "init_telethon_client", new=AsyncMock(return_value=None))
    mocker.patch.object(main_module, "setup_bot_commands", new=AsyncMock())
    mocker.patch("src.core.health.get_health_checker")
    mocker.patch("src.core.i18n.get_resolver")
    mocker.patch("src.core.i18n.get_translator")
    curfew_scheduler = MagicMock()
    curfew_scheduler.start = AsyncMock()
    mocker.patch(
        "src.services.curfew_scheduler.get_curfew_scheduler", return_value=curfew_scheduler
    )
    # 数据清理调度器同样必须 mock：真实启动会连真实 DB 执行清理并泄漏后台任务
    cleanup_service = MagicMock()
    cleanup_service.start = AsyncMock()
    mocker.patch("src.services.data_cleanup.get_data_cleanup_service", return_value=cleanup_service)


async def test_on_startup_schedules_resume_without_blocking(mocker) -> None:
    """on_startup 派发恢复任务且不 await（不阻塞启动）。"""
    resume_mock = AsyncMock(return_value=0)
    _patch_on_startup_dependencies(mocker, resume_mock)

    await main.on_startup(MagicMock())
    # 让 create_task 派发的后台任务跑完
    await asyncio.sleep(0)

    resume_mock.assert_awaited_once()


async def test_on_startup_swallows_resume_scheduling_failure(mocker) -> None:
    """恢复函数自身抛错时 on_startup 不受影响，正常完成。"""
    resume_mock = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    _patch_on_startup_dependencies(mocker, resume_mock)

    # 不应抛出（后台任务异常由 asyncio 记录，不阻断启动流程）
    await main.on_startup(MagicMock())
    await asyncio.sleep(0)


async def test_resume_empty_scan_returns_zero(mocker) -> None:
    """无 deadline 键时返回 0，不派发任务。"""
    redis = _patch_redis(mocker, [], {})
    created = _patch_task_runner(mocker)

    resumed = await handler.resume_pending_verification_timeouts(MagicMock())

    assert resumed == 0
    assert created == []
    redis.scan_iter.assert_called_once_with(match="verification_deadline:*", count=100)


async def test_resume_survives_scan_level_failure(mocker) -> None:
    """SCAN 级异常不外抛（不影响启动主流程），返回已派发数。"""
    redis = MagicMock()
    redis.scan_iter = MagicMock(side_effect=RuntimeError("redis down"))
    mocker.patch.object(handler, "get_redis", return_value=redis)

    resumed = await handler.resume_pending_verification_timeouts(MagicMock())

    assert resumed == 0


async def test_resume_retries_transient_dependency_failure(mocker) -> None:
    """依赖瞬时故障（Redis GET 抖动）经补扫恢复，不丢会话。"""
    join_timeout = mocker.patch.object(handler, "handle_verification_timeout", new=AsyncMock())

    join_key = RedisKeys.verification_deadline(CHAT_ID, JOIN_USER_ID)
    type_key = RedisKeys.verification_type(CHAT_ID, JOIN_USER_ID)
    values = {
        join_key: "session-join:9999999999999",
        type_key: "join",
    }

    call_count = {"n": 0}

    async def _flaky_get(key):
        # 首次访问抛错模拟启动抖动，补扫时恢复
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("connection reset")
        return values.get(key)

    redis = MagicMock()

    async def _scan_iter(**_kwargs):
        yield join_key

    redis.scan_iter = MagicMock(side_effect=_scan_iter)
    redis.get = _flaky_get
    mocker.patch.object(handler, "get_redis", return_value=redis)
    _patch_group_repo(mocker, None)
    created = _patch_task_runner(mocker)

    resumed = await handler.resume_pending_verification_timeouts(MagicMock())
    await asyncio.gather(*created)

    assert resumed == 1
    join_timeout.assert_awaited_once()
