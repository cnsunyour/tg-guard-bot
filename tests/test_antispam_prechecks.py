"""on_* 处理器统一前置过滤测试。

覆盖：
- ``_run_message_prechecks``：SkipReason 各分支、短路顺序、副作用（username 映射/admin）位置
- ``_is_registered_command``：命令解析各形态（含 @botname 不校验语义）
- ``update_username_mapping_if_needed``：best-effort 异常吞咽
- ``on_message`` / ``on_photo`` 对 ADMIN 的上下文记录差异（仅 on_message 记录）
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
    return message


def _stub_precheck_deps(mocker, *, channel: bool = False, is_admin: bool = False):
    """mock 掉 _run_message_prechecks 的外部依赖，返回 (channel, username, admin) 供断言。"""
    channel_mock = mocker.patch.object(
        antispam, "check_and_handle_channel_as_sender", new=AsyncMock(return_value=channel)
    )
    username_mock = mocker.patch.object(
        antispam, "update_username_mapping_if_needed", new=AsyncMock()
    )
    admin_mock = mocker.patch.object(
        antispam, "check_admin_permission_by_id", new=AsyncMock(return_value=is_admin)
    )
    return channel_mock, username_mock, admin_mock


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
    channel, username, admin = _stub_precheck_deps(mocker)

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
    channel, username, admin = _stub_precheck_deps(mocker)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.ANONYMOUS
    channel.assert_not_awaited()
    username.assert_not_awaited()
    admin.assert_not_awaited()


async def test_prechecks_channel_handled(mocker) -> None:
    """频道马甲已消费 → CHANNEL_HANDLED（不进 from_user/username/admin）"""
    message = _message(from_user=SimpleNamespace(id=42, username=None))
    _, username, admin = _stub_precheck_deps(mocker, channel=True)

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
    _, username, admin = _stub_precheck_deps(mocker, channel=False)

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
    _, username, admin = _stub_precheck_deps(mocker, channel=False)

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
    _, username, admin = _stub_precheck_deps(mocker, channel=False, is_admin=False)

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
    _stub_precheck_deps(mocker, channel=False, is_admin=True)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is antispam.SkipReason.ADMIN


async def test_prechecks_all_pass_returns_none(mocker) -> None:
    """全部通过 → None（调用方继续业务处理）"""
    message = _message(from_user=SimpleNamespace(id=42, username="u"))
    _stub_precheck_deps(mocker, channel=False, is_admin=False)

    result = await antispam._run_message_prechecks(message, MagicMock())

    assert result is None


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
    _, username, admin = _stub_precheck_deps(mocker, channel=True)

    result = await antispam._run_message_prechecks(
        message, MagicMock(), skip_registered_commands=True
    )

    assert result is antispam.SkipReason.CHANNEL_HANDLED  # 不是 REGISTERED_COMMAND
    username.assert_not_awaited()
    admin.assert_not_awaited()


# ===== 11 处处理器委托契约（参数化）=====

# handler 名 → 是否传 skip_registered_commands=True
_HANDLER_DELEGATION_CASES = [
    ("on_message", True),
    ("on_photo_message", False),
    ("on_sticker_message", False),
    ("on_video_message", False),
    ("on_animation_message", False),
    ("on_voice_message", False),
    ("on_video_note_message", False),
    ("on_document_message", False),
    ("on_audio_message", False),
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
    """11 处 on_* 处理器都委托 _run_message_prechecks；文本处理器传 skip_commands=True。

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
