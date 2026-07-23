"""入群处理 in-flight 互斥锁测试。

包含两部分：
- 锁工具单元测试：取锁（SET NX EX + owner token）/释放（Lua compare-and-delete）的正确性。
- 入口集成测试：on_join_request / on_user_join 的调用顺序、快速路径前移、键隔离与异常释放。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.handlers import verification as v
from src.core.redis import RedisKeys

# ==================== 测试辅助 ====================


def _make_join_request_event(
    *, chat_id: int = -100, user_id: int = 42, username: str | None = "alice"
) -> MagicMock:
    """构造 chat_join_request 事件 mock。"""
    event = MagicMock()
    event.chat.id = chat_id
    event.chat.title = "测试群"
    event.from_user.id = user_id
    event.from_user.username = username
    event.from_user.full_name = "Alice"
    return event


def _make_chat_member_event(
    *, chat_id: int = -100, user_id: int = 42, username: str | None = "alice"
) -> MagicMock:
    """构造 chat_member JOIN 事件 mock（from_user=None 表示普通加入、无邀请者）。"""
    event = MagicMock()
    event.chat.id = chat_id
    event.chat.title = "测试群"
    event.from_user = None
    event.new_chat_member.user.id = user_id
    event.new_chat_member.user.username = username
    event.new_chat_member.user.full_name = "Alice"
    return event


# ==================== 锁工具单元测试 ====================


def test_inflight_keys_are_independent() -> None:
    """join_request_inflight 与 join_inflight 使用独立键，避免两入口互相误锁。"""
    assert RedisKeys.join_request_inflight(-100, 42) == "join_request_inflight:-100:42"
    assert RedisKeys.join_inflight(-100, 42) == "join_inflight:-100:42"
    assert RedisKeys.join_request_inflight(-100, 42) != RedisKeys.join_inflight(-100, 42)


async def test_inflight_lock_acquires_and_releases_with_token() -> None:
    """取锁成功：SET NX EX 写入 owner token，退出时按同一 token 用 Lua 删除。"""
    redis = AsyncMock()
    redis.set.return_value = True

    with patch.object(v, "get_redis", return_value=redis):
        async with v._verification_inflight_lock("join_request_inflight:-100:42") as acquired:
            assert acquired is True

    lock_token = redis.set.await_args.args[1]
    redis.set.assert_awaited_once_with(
        "join_request_inflight:-100:42",
        lock_token,
        nx=True,
        ex=v.settings.verification_inflight_ttl_seconds,
    )
    redis.eval.assert_awaited_once_with(
        v._INFLIGHT_RELEASE_SCRIPT, 1, "join_request_inflight:-100:42", lock_token
    )


async def test_inflight_lock_skips_when_already_held() -> None:
    """取锁失败（已有处理在进行）：yield False 且不调用释放脚本，避免误删他人的锁。"""
    redis = AsyncMock()
    redis.set.return_value = False

    with patch.object(v, "get_redis", return_value=redis):
        async with v._verification_inflight_lock("join_inflight:-100:42") as acquired:
            assert acquired is False

    redis.eval.assert_not_awaited()


async def test_inflight_lock_releases_even_on_exception() -> None:
    """处理过程中抛异常也必须释放锁，防止锁泄漏导致后续请求被永久跳过。"""
    redis = AsyncMock()
    redis.set.return_value = True

    with patch.object(v, "get_redis", return_value=redis):
        try:
            async with v._verification_inflight_lock("join_request_inflight:-100:42"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    redis.eval.assert_awaited_once()


# ==================== 入口集成测试 ====================


async def test_on_join_request_skips_processing_when_inflight_held() -> None:
    """inflight 锁已被持有时，on_join_request 不进入处理流程（防重入核心）。"""
    event = _make_join_request_event()
    bot = AsyncMock()

    redis = AsyncMock()

    async def set_by_key(key, *args, **kwargs):
        # dedup 放行，inflight 已被持有
        return not key.startswith("join_request_inflight:")

    redis.set = AsyncMock(side_effect=set_by_key)

    with (
        patch.object(v, "get_redis", return_value=redis),
        patch.object(v, "_process_join_request", new=AsyncMock()) as proc_mock,
        patch.object(v.UsernameMappingService, "update_mapping", new=AsyncMock()),
    ):
        await v.on_join_request(event, bot)

    proc_mock.assert_not_called()
    # 未取得锁，不应调用释放脚本
    redis.eval.assert_not_awaited()


async def test_process_join_request_skips_checks_when_pending() -> None:
    """pending 命中时走快速路径，CAS/状态/AI 均不被调用（前移核心）。"""
    event = _make_join_request_event()
    bot = AsyncMock()

    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)  # approved 不存在

    verification_service = AsyncMock()
    verification_service.is_verification_pending = AsyncMock(return_value=True)

    with (
        patch.object(v.settings, "cas_enabled", True),
        patch.object(v.settings, "user_status_check_enabled", True),
        patch.object(v, "get_redis", return_value=redis),
        patch.object(v, "VerificationService", return_value=verification_service),
        patch.object(v, "get_cas_service", new=AsyncMock()) as cas_mock,
        patch.object(v, "get_user_status_service", new=AsyncMock()) as status_mock,
        patch.object(v, "check_user_spam_info", new=AsyncMock()) as ai_mock,
    ):
        await v._process_join_request(event, bot, -100, 42, "alice")

    cas_mock.assert_not_called()
    status_mock.assert_not_called()
    ai_mock.assert_not_called()


async def test_process_join_request_uses_approved_fast_path() -> None:
    """approved 命中时走批准快速路径，不调用 CAS/AI。"""
    event = _make_join_request_event()
    bot = AsyncMock()

    redis = AsyncMock()
    redis.get = AsyncMock(return_value="1")  # approved 存在

    verification_service = AsyncMock()
    verification_service.is_verification_pending = AsyncMock(return_value=False)

    with (
        patch.object(v.settings, "cas_enabled", True),
        patch.object(v, "get_redis", return_value=redis),
        patch.object(v, "VerificationService", return_value=verification_service),
        patch.object(v, "get_cas_service", new=AsyncMock()) as cas_mock,
        patch.object(v, "check_user_spam_info", new=AsyncMock()) as ai_mock,
        patch.object(v, "_handle_approved_join_request", new=AsyncMock()) as approved_mock,
    ):
        await v._process_join_request(event, bot, -100, 42, "alice")

    approved_mock.assert_awaited_once_with(bot, -100, 42)
    cas_mock.assert_not_called()
    ai_mock.assert_not_called()


async def test_process_user_join_restricts_before_pending_check() -> None:
    """on_user_join 先 restrict 再检查 pending：pending 命中时用户已被限制权限。"""
    event = _make_chat_member_event()
    bot = AsyncMock()

    redis = AsyncMock()

    verification_service = AsyncMock()
    verification_service.is_verification_pending = AsyncMock(return_value=True)

    with (
        patch.object(v, "get_redis", return_value=redis),
        patch.object(v, "VerificationService", return_value=verification_service),
    ):
        await v._process_user_join(event, bot, -100, 42, "alice")

    bot.restrict_chat_member.assert_awaited_once()


async def test_on_join_request_uses_join_request_inflight_key() -> None:
    """on_join_request 使用 join_request_inflight 键取锁。"""
    event = _make_join_request_event()
    bot = AsyncMock()

    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)

    with (
        patch.object(v, "get_redis", return_value=redis),
        patch.object(v, "_process_join_request", new=AsyncMock()),
        patch.object(v.UsernameMappingService, "update_mapping", new=AsyncMock()),
    ):
        await v.on_join_request(event, bot)

    inflight_keys = [
        str(call.args[0])
        for call in redis.set.await_args_list
        if call.args and str(call.args[0]).startswith("join_request_inflight:")
    ]
    assert inflight_keys == [RedisKeys.join_request_inflight(-100, 42)]


async def test_on_user_join_uses_join_inflight_key() -> None:
    """on_user_join 使用 join_inflight 键取锁（与加入请求入口隔离）。"""
    event = _make_chat_member_event()
    bot = AsyncMock()

    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock()

    with (
        patch.object(v, "get_redis", return_value=redis),
        patch.object(v, "_process_user_join", new=AsyncMock()),
        patch.object(v.UsernameMappingService, "update_mapping", new=AsyncMock()),
    ):
        await v.on_user_join(event, bot)

    inflight_keys = [
        str(call.args[0])
        for call in redis.set.await_args_list
        if call.args and str(call.args[0]).startswith("join_inflight:")
    ]
    assert inflight_keys == [RedisKeys.join_inflight(-100, 42)]


async def test_on_join_request_releases_lock_and_propagates_on_error() -> None:
    """_process_join_request 抛异常时：锁被释放，且异常向上冒泡（不被外壳吞掉）。"""
    event = _make_join_request_event()
    bot = AsyncMock()

    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)

    with (
        patch.object(v, "get_redis", return_value=redis),
        patch.object(
            v,
            "_process_join_request",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(v.UsernameMappingService, "update_mapping", new=AsyncMock()),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await v.on_join_request(event, bot)

    redis.eval.assert_awaited_once()
