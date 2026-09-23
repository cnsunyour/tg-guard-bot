"""on_* 处理器统一前置过滤测试。

覆盖：
- ``_run_message_prechecks``：SkipReason 各分支、短路顺序、副作用（username 映射/admin）位置
- ``_is_registered_command``：命令解析各形态（含 @botname 不校验语义）
- ``update_username_mapping_if_needed``：best-effort 异常吞咽
- ``on_message`` / ``on_photo`` 对 ADMIN 的上下文记录差异（仅 on_message 记录）
- ``on_activity_only_message``：富媒体 + 结构化消息统一走活跃度门槛
- ``live_photo`` 复用 photo 链路（预览尺寸回退、缺预览时跳过 Vision）
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam

pytestmark = pytest.mark.unit

CHAT_ID = -100123


@pytest.fixture(autouse=True)
def _isolate_registered_commands() -> None:
    """每个测试前保存、后恢复 ``_registered_commands``，防全局状态泄漏。"""
    original = antispam.get_registered_commands()
    yield
    antispam.set_registered_commands(original)


def _message(
    *,
    chat_type: str = "supergroup",
    sender_chat: object | None = None,
    from_user: object | None = None,
    text: str | None = None,
    content_type: str = "text",
) -> MagicMock:
    """构造 on_* 处理器入参 Message mock。"""
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID, type=chat_type, title="Test")
    message.sender_chat = sender_chat
    message.from_user = from_user
    message.text = text
    message.content_type = content_type
    message.caption = None
    message.message_id = 1
    # MagicMock 未显式设置的属性自动生成 truthy mock，跨聊天回复字段必须显式置 None
    message.external_reply = None
    message.quote = None
    return message


def _stub_precheck_deps(
    mocker, *, channel: bool = False, is_admin: bool = False, extreply: bool = False
):
    """mock 掉 _run_message_prechecks 的外部依赖，返回 (channel, username, admin, extreply)。"""
    channel_mock = mocker.patch.object(
        antispam, "check_and_handle_channel_as_sender", new=AsyncMock(return_value=channel)
    )
    username_mock = mocker.patch.object(
        antispam, "update_username_mapping_if_needed", new=AsyncMock()
    )
    admin_mock = mocker.patch.object(
        antispam, "check_admin_permission_by_id", new=AsyncMock(return_value=is_admin)
    )
    extreply_mock = mocker.patch.object(
        antispam, "check_and_handle_external_reply", new=AsyncMock(return_value=extreply)
    )
    return channel_mock, username_mock, admin_mock, extreply_mock


# ===== _is_registered_command =====


def test_is_registered_command_plain():
    """已注册命令 → True"""
    antispam.set_registered_commands({"antispam", "lang"})
    assert antispam._is_registered_command(_message(text="/antispam")) is True


def test_is_registered_command_with_args():
    """带参数 → True"""
    antispam.set_registered_commands({"antispam"})
    assert antispam._is_registered_command(_message(text="/antispam on")) is True


def test_is_registered_command_with_botname():
    """带 @botname → True（不校验是否本 bot，保持现有行为）"""
    antispam.set_registered_commands({"antispam"})
    assert antispam._is_registered_command(_message(text="/antispam@other_bot")) is True


def test_is_registered_command_unregistered():
    """未注册命令格式 → False（继续垃圾检测）"""
    antispam.set_registered_commands({"antispam"})
    assert antispam._is_registered_command(_message(text="/abc spam")) is False


def test_is_registered_command_invalid_format():
    """非法命令格式（/数字开头）→ False"""
    antispam.set_registered_commands({"antispam"})
    assert antispam._is_registered_command(_message(text="/123abc")) is False


def test_is_registered_command_plain_text():
    """普通文本 → False"""
    antispam.set_registered_commands({"antispam"})
    assert antispam._is_registered_command(_message(text="hello world")) is False


def test_is_registered_command_empty_text():
    """空文本 / None → False"""
    antispam.set_registered_commands({"antispam"})
    assert antispam._is_registered_command(_message(text=None)) is False
    assert antispam._is_registered_command(_message(text="")) is False


# ===== _run_message_prechecks: 各分支与短路顺序 =====


async def test_prechecks_private_short_circuits(mocker) -> None:
    """私聊 → PRIVATE，后续依赖均不调用"""
    message = _message(chat_type="private", from_user=SimpleNamespace(id=42, username=None))
    channel, username, admin, _ = _stub_precheck_deps(mocker)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.PRIVATE
    channel.assert_not_awaited()
    username.assert_not_awaited()
    admin.assert_not_awaited()


async def test_prechecks_anonymous_before_channel(mocker) -> None:
    """匿名管理员在频道检测前短路（sender_chat == chat.id）"""
    message = _message(
        sender_chat=SimpleNamespace(id=CHAT_ID),
        from_user=SimpleNamespace(id=1087968824, username=None),
    )
    channel, username, admin, _ = _stub_precheck_deps(mocker)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.ANONYMOUS
    channel.assert_not_awaited()
    username.assert_not_awaited()
    admin.assert_not_awaited()


async def test_prechecks_channel_handled(mocker) -> None:
    """频道马甲已消费 → CHANNEL_HANDLED（不进 from_user/username/admin）"""
    message = _message(from_user=SimpleNamespace(id=42, username=None))
    _, username, admin, _ = _stub_precheck_deps(mocker, channel=True)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.CHANNEL_HANDLED
    username.assert_not_awaited()
    admin.assert_not_awaited()


async def test_prechecks_channel_before_no_from_user(mocker) -> None:
    """频道检测在 from_user 前：频道身份消息即使无 from_user 也被消费"""
    message = _message(from_user=None)
    _stub_precheck_deps(mocker, channel=True)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.CHANNEL_HANDLED


async def test_prechecks_no_from_user(mocker) -> None:
    """频道未消费 + 无 from_user → NO_FROM_USER"""
    message = _message(from_user=None)
    _, username, admin, _ = _stub_precheck_deps(mocker, channel=False)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.NO_FROM_USER
    username.assert_not_awaited()
    admin.assert_not_awaited()


async def test_prechecks_registered_command_skips_when_enabled(mocker) -> None:
    """skip_commands=True + 已注册命令 → REGISTERED_COMMAND（username/admin 不调用）"""
    message = _message(
        from_user=SimpleNamespace(id=42, username="u"),
        text="/antispam",
    )
    antispam.set_registered_commands({"antispam"})
    _, username, admin, _ = _stub_precheck_deps(mocker, channel=False)

    result = await antispam._run_message_prechecks(
        message, MagicMock(), skip_registered_commands=True
    )

    assert result is antispam.SkipReason.REGISTERED_COMMAND
    username.assert_not_awaited()
    admin.assert_not_awaited()


async def test_prechecks_command_passes_when_skip_disabled(mocker) -> None:
    """skip_commands=False + 命令文本 → 通过（None），正常走 username/admin"""
    message = _message(
        from_user=SimpleNamespace(id=42, username="u"),
        text="/antispam",
    )
    antispam.set_registered_commands({"antispam"})
    _, username, admin, _ = _stub_precheck_deps(mocker, channel=False, is_admin=False)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is None
    username.assert_awaited_once()
    admin.assert_awaited_once()


async def test_prechecks_username_mapping_before_admin(mocker) -> None:
    """username 映射先于 admin 检查（用 parent mock 验证调用顺序）"""
    parent = MagicMock()
    parent.username = AsyncMock()
    parent.admin = AsyncMock(return_value=False)
    mocker.patch.object(antispam, "update_username_mapping_if_needed", parent.username)
    mocker.patch.object(antispam, "check_admin_permission_by_id", parent.admin)
    mocker.patch.object(
        antispam, "check_and_handle_channel_as_sender", new=AsyncMock(return_value=False)
    )
    message = _message(from_user=SimpleNamespace(id=42, username="u"))

    await antispam._run_message_prechecks(message, MagicMock())

    call_names = [c[0] for c in parent.mock_calls]
    assert "username" in call_names and "admin" in call_names
    assert call_names.index("username") < call_names.index("admin")


async def test_prechecks_admin_exemption(mocker) -> None:
    """管理员 → ADMIN"""
    message = _message(from_user=SimpleNamespace(id=42, username="u"))
    _, _, _, extreply = _stub_precheck_deps(mocker, channel=False, is_admin=True)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.ADMIN
    extreply.assert_not_awaited()  # 管理员豁免先于跨聊天回复检测


async def test_prechecks_all_pass_returns_none(mocker) -> None:
    """全部通过 → None（调用方继续业务处理）"""
    message = _message(from_user=SimpleNamespace(id=42, username="u"))
    _stub_precheck_deps(mocker, channel=False, is_admin=False)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is None


async def test_prechecks_external_reply_handled(mocker) -> None:
    """跨聊天回复已消费 → EXTERNAL_REPLY_HANDLED（第 8 步，管理员豁免之后）"""
    message = _message(from_user=SimpleNamespace(id=42, username="u"))
    bot = MagicMock()
    _, _, _, extreply = _stub_precheck_deps(mocker, channel=False, is_admin=False, extreply=True)

    result = await antispam._run_message_prechecks(message, bot)

    assert result is antispam.SkipReason.EXTERNAL_REPLY_HANDLED
    extreply.assert_awaited_once_with(message, bot)


# ===== update_username_mapping_if_needed: best-effort =====


async def test_username_mapping_swallows_exception(mocker) -> None:
    """Redis 异常：update_mapping 被调用但不向上传播（best-effort）"""
    update = mocker.patch.object(
        antispam.UsernameMappingService,
        "update_mapping",
        new=AsyncMock(side_effect=Exception("redis down")),
    )
    message = _message(from_user=SimpleNamespace(id=42, username="u"))

    await antispam.update_username_mapping_if_needed(message)  # 不抛异常

    update.assert_awaited_once_with(user_id=42, username="u")


async def test_username_mapping_success(mocker) -> None:
    """成功路径：调用 update_mapping 传入正确参数"""
    update = mocker.patch.object(antispam.UsernameMappingService, "update_mapping", new=AsyncMock())
    message = _message(from_user=SimpleNamespace(id=42, username="u"))

    await antispam.update_username_mapping_if_needed(message)

    update.assert_awaited_once_with(user_id=42, username="u")


async def test_username_mapping_skips_when_no_username(mocker) -> None:
    """无 username 不调 update_mapping"""
    update = mocker.patch.object(antispam.UsernameMappingService, "update_mapping", new=AsyncMock())
    message = _message(from_user=SimpleNamespace(id=42, username=None))

    await antispam.update_username_mapping_if_needed(message)
    update.assert_not_awaited()


# ===== on_message / on_photo 对 ADMIN 的上下文记录差异 =====


async def test_on_message_admin_records_context(mocker) -> None:
    """on_message 收到 ADMIN 时记录管理员消息到上下文（独有行为）"""
    message = _message(text="admin msg", from_user=SimpleNamespace(id=42, username=None))
    mocker.patch.object(
        antispam,
        "_run_message_prechecks",
        new=AsyncMock(return_value=antispam.SkipReason.ADMIN),
    )
    record = mocker.patch.object(antispam.ContextService, "record_message", new=AsyncMock())

    await antispam.on_message(message, MagicMock())

    record.assert_awaited_once_with(message)


async def test_on_photo_admin_does_not_record_context(mocker) -> None:
    """on_photo 收到 ADMIN 时不记录上下文（仅 on_message 记录）"""
    message = _message(
        content_type="photo",
        from_user=SimpleNamespace(id=42, username=None),
    )
    message.photo = []
    mocker.patch.object(
        antispam,
        "_run_message_prechecks",
        new=AsyncMock(return_value=antispam.SkipReason.ADMIN),
    )
    record = mocker.patch.object(antispam.ContextService, "record_message", new=AsyncMock())

    await antispam.on_photo_message(message, MagicMock())

    record.assert_not_awaited()


# ===== 频道身份发命令的安全顺序（本次修复核心）=====


async def test_prechecks_channel_before_registered_command(mocker) -> None:
    """频道身份发已注册命令仍先走 anti-channel（命令检查在频道之后）。

    锁定本次安全修复的核心顺序：命令判断不得前移到频道检查之前，否则频道
    马甲发 ``/help`` 等命令可绕过 anti-channel 检测。
    """
    message = _message(
        from_user=SimpleNamespace(id=42, username="u"),
        text="/antispam",
    )
    antispam.set_registered_commands({"antispam"})
    _, username, admin, _ = _stub_precheck_deps(mocker, channel=True)

    result = await antispam._run_message_prechecks(
        message, MagicMock(), skip_registered_commands=True
    )

    assert result is antispam.SkipReason.CHANNEL_HANDLED  # 不是 REGISTERED_COMMAND
    username.assert_not_awaited()
    admin.assert_not_awaited()


# ===== on_* 处理器委托契约（参数化）=====

# handler 名 → 是否传 skip_registered_commands=True
_HANDLER_DELEGATION_CASES = [
    ("on_message", True),
    ("on_photo_message", False),
    ("on_sticker_message", False),
    ("on_activity_only_message", False),
    ("on_edited_text_message", True),
    ("on_edited_photo_message", False),
]


@pytest.mark.parametrize(
    ("handler_name", "skip_commands"),
    _HANDLER_DELEGATION_CASES,
    ids=[name for name, _ in _HANDLER_DELEGATION_CASES],
)
async def test_handler_delegates_to_prechecks(
    handler_name: str, skip_commands: bool, mocker
) -> None:
    """全部 on_* 处理器都委托 _run_message_prechecks；文本处理器传 skip_commands=True。

    mock 公共前置返回 PRIVATE（静默跳过），处理器应在前置后直接 return，
    不进入 group/活跃度/检测等业务逻辑——从而锁定委托契约，防止未来某处理器
    重新出现独立前置。
    """
    prechecks = AsyncMock(return_value=antispam.SkipReason.PRIVATE)
    mocker.patch.object(antispam, "_run_message_prechecks", new=prechecks)

    handler = getattr(antispam, handler_name)
    await handler(_message(), MagicMock())

    prechecks.assert_awaited_once()
    expected_kwargs = {"skip_registered_commands": True} if skip_commands else {}
    assert prechecks.await_args.kwargs == expected_kwargs


# ===== 只做活跃度检查的非文本消息（富媒体 + 结构化消息）=====

_ACTIVITY_ONLY_CONTENT_TYPES = [
    # 富媒体
    "video",
    "animation",
    "voice",
    "video_note",
    "document",
    "audio",
    # 结构化消息：无文本载荷，但必须有 handler 才能受活跃度门槛与 inner 中间件约束
    "contact",
    "poll",
    "location",
    "venue",
    "checklist",
    "story",
    "dice",
]


@pytest.mark.parametrize("content_type", _ACTIVITY_ONLY_CONTENT_TYPES)
async def test_activity_only_message_runs_non_text_check(mocker, content_type: str) -> None:
    """每种非文本类型都按 content_type 走一次活跃度检查，群开关透传。"""
    message = _message(from_user=SimpleNamespace(id=42, username=None), content_type=content_type)
    bot = MagicMock()
    mocker.patch.object(antispam, "_run_message_prechecks", new=AsyncMock(return_value=None))
    mocker.patch.object(
        antispam.GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(activity_enabled=False)),
    )
    check = mocker.patch.object(
        antispam, "check_non_text_message", new=AsyncMock(return_value=False)
    )

    await antispam.on_activity_only_message(message, bot)

    check.assert_awaited_once_with(message, bot, content_type, False)


async def test_activity_only_message_defaults_to_enabled_when_group_lookup_fails(mocker) -> None:
    """群配置读取失败时按「启用限制」保守处理（与既有富媒体 handler 行为一致）。"""
    message = _message(from_user=SimpleNamespace(id=42, username=None), content_type="dice")
    bot = MagicMock()
    mocker.patch.object(antispam, "_run_message_prechecks", new=AsyncMock(return_value=None))
    mocker.patch.object(
        antispam.GroupRepository, "get", new=AsyncMock(side_effect=RuntimeError("db down"))
    )
    check = mocker.patch.object(
        antispam, "check_non_text_message", new=AsyncMock(return_value=False)
    )

    await antispam.on_activity_only_message(message, bot)

    check.assert_awaited_once_with(message, bot, "dice", True)


async def test_activity_only_filter_covers_structured_types() -> None:
    """路由过滤器必须覆盖全部结构化类型：漏掉任一类型即绕过活跃度门槛与 inner 中间件。"""
    handler = next(
        h
        for h in antispam.router.message.handlers
        if h.callback is antispam.on_activity_only_message
    )
    for content_type in _ACTIVITY_ONLY_CONTENT_TYPES:
        message = MagicMock(spec=[content_type])
        setattr(message, content_type, object())
        matched, _ = await handler.check(message)
        assert matched, f"{content_type} 未被 on_activity_only_message 过滤器覆盖"

    # 反向：文本消息不得落入本 handler
    text_message = MagicMock(spec=["text"])
    text_message.text = "hello"
    matched, _ = await handler.check(text_message)
    assert not matched


# ===== live_photo 走 photo 链路 =====


def test_photo_sizes_prefers_photo() -> None:
    sizes = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")]
    message = SimpleNamespace(photo=sizes, live_photo=None)

    assert antispam._photo_sizes(message) == sizes


def test_photo_sizes_falls_back_to_live_photo_preview() -> None:
    sizes = [SimpleNamespace(file_id="preview")]
    message = SimpleNamespace(photo=None, live_photo=SimpleNamespace(photo=sizes))

    assert antispam._photo_sizes(message) == sizes


def test_photo_sizes_empty_when_live_photo_has_no_preview() -> None:
    message = SimpleNamespace(photo=None, live_photo=SimpleNamespace(photo=None))

    assert antispam._photo_sizes(message) == []


def _stub_photo_pipeline(mocker, *, activity_blocked: bool = False):
    """mock on_photo_message 的群配置 / 活跃度 / 检测器依赖，返回 (check, detector)。"""
    mocker.patch.object(antispam, "_run_message_prechecks", new=AsyncMock(return_value=None))
    mocker.patch.object(
        antispam.GroupRepository,
        "get_or_create",
        new=AsyncMock(
            return_value=SimpleNamespace(
                antispam_enabled=True,
                activity_enabled=True,
                activity_skip_threshold=0,
                spam_confirm_enabled=False,
            )
        ),
    )
    check = mocker.patch.object(
        antispam, "check_non_text_message", new=AsyncMock(return_value=activity_blocked)
    )
    mocker.patch.object(antispam.ActivityService, "get_activity", new=AsyncMock(return_value=0))
    mocker.patch.object(antispam.settings, "activity_skip_spam_check_threshold", 0)
    mocker.patch.object(antispam.settings, "context_enabled", False)
    detector = MagicMock()
    detector.detect_images = AsyncMock(return_value={"is_spam": False})
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    return check, detector


async def test_live_photo_downloads_preview_and_runs_vision(mocker) -> None:
    """live_photo 复用 photo 链路：活跃度按 live_photo 记类型，下载最大预览尺寸交 Vision。"""
    preview = SimpleNamespace(file_id="preview-large")
    message = _message(from_user=SimpleNamespace(id=42, username=None), content_type="live_photo")
    message.photo = None
    message.live_photo = SimpleNamespace(photo=[SimpleNamespace(file_id="preview-small"), preview])
    bot = MagicMock()
    bot.download = AsyncMock()
    check, detector = _stub_photo_pipeline(mocker)

    await antispam.on_photo_message(message, bot)

    check.assert_awaited_once_with(message, bot, "live_photo", True)
    assert bot.download.await_args.args[0] is preview
    detector.detect_images.assert_awaited_once()


async def test_live_photo_without_preview_skips_vision_after_activity_check(mocker) -> None:
    """预览缺失时不能拿视频 file_id 当图片下载：活跃度检查照做，视觉检测跳过。"""
    message = _message(from_user=SimpleNamespace(id=42, username=None), content_type="live_photo")
    message.photo = None
    message.live_photo = SimpleNamespace(photo=None)
    bot = MagicMock()
    bot.download = AsyncMock()
    check, detector = _stub_photo_pipeline(mocker)

    await antispam.on_photo_message(message, bot)

    check.assert_awaited_once()
    bot.download.assert_not_awaited()
    detector.detect_images.assert_not_awaited()
