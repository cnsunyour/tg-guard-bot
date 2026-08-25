"""check_user_spam_info 的 bio 获取链测试

背景（tdlib/telegram-bot-api#839）：Bot API getChat 仅对曾与 Bot 私聊交互过的
用户返回 bio，入群验证场景下大多拿不到。修复后：
- join_request：ChatJoinRequest 事件自带 bio 为权威来源，不再额外获取
- join：ChatMemberUpdated 无 bio 字段，按 Telethon full user → getChat 顺序补齐，
  两级都失败则只检测名字（不阻断流程）
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.handlers import verification as v
from src.services import user_status_service as us


def _detector() -> MagicMock:
    """SpamDetector mock：detect_with_ai 默认返回非垃圾结果。"""
    detector = MagicMock()
    detector.detect_with_ai = AsyncMock(
        return_value={"is_spam": False, "confidence": 0.0, "reasons": []}
    )
    return detector


def _status_service(*, bio: str | None = None, error: Exception | None = None) -> MagicMock:
    """UserStatusService mock：控制 get_user_bio 的返回或异常。"""
    service = MagicMock()
    if error is not None:
        service.get_user_bio = AsyncMock(side_effect=error)
    else:
        service.get_user_bio = AsyncMock(return_value=bio)
    return service


# ========== check_user_spam_info：检测文本组装与 bio 来源选择 ==========


async def test_join_request_uses_event_bio_without_fetch() -> None:
    """join_request 事件自带 bio 与名字，不再调用 Telethon/getChat。"""
    bot = AsyncMock()
    detector = _detector()
    status_service = _status_service()

    with (
        patch.object(v, "get_user_status_service", return_value=status_service),
        patch.object(v, "SpamDetector", return_value=detector),
    ):
        result = await v.check_user_spam_info(
            bot,
            -100,
            42,
            "alice",
            "join_request",
            first_name="Alice",
            last_name="Doe",
            bio="事件简介",
        )

    assert result is False
    status_service.get_user_bio.assert_not_awaited()
    bot.get_chat.assert_not_awaited()
    assert detector.detect_with_ai.await_args.kwargs["text"] == "Alice Doe 事件简介"


async def test_join_request_without_bio_skips_fetch() -> None:
    """join_request 事件未携带 bio（用户未设置）时不做任何补齐获取。"""
    bot = AsyncMock()
    detector = _detector()
    status_service = _status_service()

    with (
        patch.object(v, "get_user_status_service", return_value=status_service),
        patch.object(v, "SpamDetector", return_value=detector),
    ):
        result = await v.check_user_spam_info(
            bot, -100, 42, "alice", "join_request", first_name="Alice"
        )

    assert result is False
    status_service.get_user_bio.assert_not_awaited()
    bot.get_chat.assert_not_awaited()
    assert detector.detect_with_ai.await_args.kwargs["text"] == "Alice"


async def test_join_prefers_telethon_bio() -> None:
    """join 模式 bio 缺失时优先 Telethon，成功则不再调用 getChat。"""
    bot = AsyncMock()
    detector = _detector()
    status_service = _status_service(bio="Telethon 简介")

    with (
        patch.object(v, "get_user_status_service", return_value=status_service),
        patch.object(v, "SpamDetector", return_value=detector),
    ):
        result = await v.check_user_spam_info(bot, -100, 42, "alice", "join", first_name="Alice")

    assert result is False
    status_service.get_user_bio.assert_awaited_once_with(-100, 42)
    bot.get_chat.assert_not_awaited()
    assert detector.detect_with_ai.await_args.kwargs["text"] == "Alice Telethon 简介"


async def test_join_falls_back_to_get_chat_when_telethon_empty() -> None:
    """Telethon 未拿到 bio（未启用/不在群组/用户无 bio）→ getChat 兜底。"""
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(bio="getChat 简介")
    detector = _detector()
    status_service = _status_service(bio=None)

    with (
        patch.object(v, "get_user_status_service", return_value=status_service),
        patch.object(v, "SpamDetector", return_value=detector),
    ):
        result = await v.check_user_spam_info(bot, -100, 42, "alice", "join", first_name="Alice")

    assert result is False
    bot.get_chat.assert_awaited_once_with(42)
    assert detector.detect_with_ai.await_args.kwargs["text"] == "Alice getChat 简介"


async def test_join_falls_back_to_get_chat_on_telethon_error() -> None:
    """Telethon 抛异常（服务层已捕获，模拟外层意外泄漏）→ getChat 兜底。"""
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(bio="getChat 简介")
    detector = _detector()
    status_service = _status_service(error=RuntimeError("telethon unavailable"))

    with (
        patch.object(v, "get_user_status_service", return_value=status_service),
        patch.object(v, "SpamDetector", return_value=detector),
    ):
        result = await v.check_user_spam_info(bot, -100, 42, "alice", "join", first_name="Alice")

    assert result is False
    bot.get_chat.assert_awaited_once_with(42)
    assert detector.detect_with_ai.await_args.kwargs["text"] == "Alice getChat 简介"


async def test_join_uses_names_only_when_all_bio_sources_fail() -> None:
    """两级 bio 来源都失败 → 只用名字检测，流程不中断。"""
    bot = AsyncMock()
    bot.get_chat.side_effect = RuntimeError("chat not found")
    detector = _detector()
    status_service = _status_service(bio=None)

    with (
        patch.object(v, "get_user_status_service", return_value=status_service),
        patch.object(v, "SpamDetector", return_value=detector),
    ):
        result = await v.check_user_spam_info(
            bot, -100, 42, "alice", "join", first_name="Alice", last_name="Doe"
        )

    assert result is False
    assert detector.detect_with_ai.await_args.kwargs["text"] == "Alice Doe"


async def test_join_without_any_texts_returns_false_without_detect() -> None:
    """名字与 bio 全空（如已删除账号）→ 跳过检测直接放行。"""
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(bio=None)
    detector = _detector()
    status_service = _status_service(bio=None)

    with (
        patch.object(v, "get_user_status_service", return_value=status_service),
        patch.object(v, "SpamDetector", return_value=detector),
    ):
        result = await v.check_user_spam_info(bot, -100, 42, "alice", "join")

    assert result is False
    detector.detect_with_ai.assert_not_awaited()


# ========== 两个入群处理流程对事件资料的透传 ==========


def _join_request_event() -> MagicMock:
    event = MagicMock()
    event.chat.title = "测试群"
    event.from_user.first_name = "Alice"
    event.from_user.last_name = "Doe"
    event.bio = "事件简介"
    return event


def _chat_member_event() -> MagicMock:
    event = MagicMock()
    event.chat.title = "测试群"
    event.from_user = None  # 跳过管理员邀请分支
    event.new_chat_member.user.id = 42
    event.new_chat_member.user.first_name = "Alice"
    event.new_chat_member.user.last_name = "Doe"
    return event


def _fast_path_patches(check: AsyncMock) -> dict:
    """_process_* 的公共 mock：跳过快速路径与外部依赖，聚焦透传断言。"""
    redis = AsyncMock()
    redis.get.return_value = None
    verification_service = AsyncMock()
    verification_service.is_verification_pending.return_value = False
    return {
        "get_redis": patch.object(v, "get_redis", return_value=redis),
        "verification": patch.object(v, "VerificationService", return_value=verification_service),
        "cas": patch.object(v.settings, "cas_enabled", False),
        "status": patch.object(v.settings, "user_status_check_enabled", False),
        "check": patch.object(v, "check_user_spam_info", new=check),
        "group_repo": patch.object(
            v.GroupRepository, "get_or_create", new=AsyncMock(return_value=MagicMock())
        ),
        "start": patch.object(v, "_start_initial_verification", new=AsyncMock(return_value="sent")),
    }


async def test_process_join_request_passes_event_profile() -> None:
    """_process_join_request 透传事件自带的名字与 bio。"""
    event = _join_request_event()
    bot = AsyncMock()
    check = AsyncMock(return_value=False)

    patches = _fast_path_patches(check)
    with (
        patches["get_redis"],
        patches["verification"],
        patches["cas"],
        patches["status"],
        patches["check"],
        patches["group_repo"],
        patches["start"],
    ):
        await v._process_join_request(event, bot, -100, 42, "alice")

    check.assert_awaited_once_with(
        bot,
        -100,
        42,
        "alice",
        mode="join_request",
        first_name="Alice",
        last_name="Doe",
        bio="事件简介",
    )


async def test_process_user_join_passes_user_names() -> None:
    """_process_user_join 透传入群用户的名字（bio 留空由检测函数补齐）。"""
    event = _chat_member_event()
    bot = AsyncMock()
    check = AsyncMock(return_value=False)

    patches = _fast_path_patches(check)
    with (
        patches["get_redis"],
        patches["verification"],
        patches["cas"],
        patches["status"],
        patches["check"],
        patches["group_repo"],
        patches["start"],
    ):
        await v._process_user_join(event, bot, -100, 42, "alice")

    check.assert_awaited_once_with(
        bot,
        -100,
        42,
        "alice",
        mode="join",
        first_name="Alice",
        last_name="Doe",
    )


# ========== UserStatusService.get_user_bio ==========


async def test_get_user_bio_disabled_returns_none() -> None:
    """服务未启用（开关关闭）→ 直接返回 None，不触碰 client。"""
    client = AsyncMock()
    service = us.UserStatusService(client)

    with patch.object(us.settings, "user_status_check_enabled", False):
        assert await service.get_user_bio(-100, 42) is None
    client.assert_not_awaited()


async def test_get_user_bio_returns_about() -> None:
    """成功路径：群组上下文解析实体后取 full_user.about。"""
    client = AsyncMock()
    client.return_value = SimpleNamespace(full_user=SimpleNamespace(about="用户简介"))
    service = us.UserStatusService(client)
    participant = MagicMock()
    lookup = AsyncMock(return_value=participant)
    request = object()

    with (
        patch.object(us.settings, "user_status_check_enabled", True),
        patch.object(service, "_get_participant_entity", new=lookup),
        patch.object(us, "GetFullUserRequest", return_value=request) as request_cls,
    ):
        assert await service.get_user_bio(-100, 42) == "用户简介"

    lookup.assert_awaited_once_with(-100, 42)
    request_cls.assert_called_once_with(id=participant)
    client.assert_awaited_once_with(request)


async def test_get_user_bio_returns_none_on_error() -> None:
    """Telethon 调用异常 → 返回 None（调用方降级 getChat）。"""
    client = AsyncMock(side_effect=RuntimeError("request failed"))
    service = us.UserStatusService(client)
    lookup = AsyncMock(return_value=MagicMock())

    with (
        patch.object(us.settings, "user_status_check_enabled", True),
        patch.object(service, "_get_participant_entity", new=lookup),
    ):
        assert await service.get_user_bio(-100, 42) is None


async def test_get_user_bio_returns_none_when_participant_missing() -> None:
    """群组上下文解析不到用户实体 → 返回 None，不发起 full user 请求。"""
    client = AsyncMock()
    service = us.UserStatusService(client)
    lookup = AsyncMock(return_value=None)

    with (
        patch.object(us.settings, "user_status_check_enabled", True),
        patch.object(service, "_get_participant_entity", new=lookup),
    ):
        assert await service.get_user_bio(-100, 42) is None
    client.assert_not_awaited()
