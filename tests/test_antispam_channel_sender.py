"""反频道马甲检测的放行矩阵与关联频道缓存测试。

覆盖 ``check_and_handle_channel_as_sender`` 在文案之外的分支（文案见
test_antispam_message_detect_i18n）：
- ``is_automatic_forward`` 官方字段放行，早于 sender_chat 判定且不查 DB
- 系统账号 / Bot 自身快速路径
- 群开关关闭 → False 回归正常管线
- 关联频道身份发言放行（走共享缓存函数，不再内联 get_chat）
- 关联频道查询失败 fail-punish（仍删除）
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam

pytestmark = pytest.mark.unit

CHAT_ID = -100123
LINKED_CHANNEL_ID = -100999
OTHER_CHANNEL_ID = -100555
BOT_ID = 999


def _message(
    *,
    sender_chat_id: int | None = OTHER_CHANNEL_ID,
    from_user_id: int | None = 136817688,
    is_automatic_forward: bool | None = None,
) -> MagicMock:
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID, type="supergroup", title="Test")
    message.message_id = 7
    message.is_automatic_forward = is_automatic_forward
    message.sender_chat = (
        SimpleNamespace(id=sender_chat_id, type="channel", title="Chan")
        if sender_chat_id is not None
        else None
    )
    message.from_user = (
        SimpleNamespace(id=from_user_id, username=None) if from_user_id is not None else None
    )
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=MagicMock(name="notice"))
    return message


def _stub(mocker, *, enabled: bool = True, linked: int | None = None, linked_error: bool = False):
    """打桩外部依赖，返回 (repo_get, linked_lookup, bot)。"""
    group = SimpleNamespace(anti_channel_enabled=enabled)
    repo_get = mocker.patch.object(
        antispam.GroupRepository, "get", new=AsyncMock(return_value=group)
    )
    linked_lookup = mocker.patch.object(
        antispam, "_get_linked_channel_id", new=AsyncMock(return_value=linked)
    )
    if linked_error:
        linked_lookup.return_value = None  # _get_linked_channel_id 内部已吞异常并返回 None
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}>"
    mocker.patch.object(antispam, "get_resolver")
    antispam.get_resolver.return_value.for_group = AsyncMock(return_value="zh-Hans")
    mocker.patch.object(
        antispam,
        "get_translator",
        return_value=MagicMock(for_locale=MagicMock(return_value=localizer)),
    )
    mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(antispam.ModerationService, "warn_user", new=AsyncMock())
    bot = MagicMock(id=BOT_ID)
    bot.get_chat = AsyncMock()
    return repo_get, linked_lookup, bot


# ===== 放行路径 =====


async def test_automatic_forward_skips_before_sender_chat_check(mocker) -> None:
    """is_automatic_forward=True → True；早于 sender_chat 判定，不查 DB / 不删除"""
    repo_get, _, bot = _stub(mocker)
    # sender_chat 为 None 也放行：Bot API 未承诺自动转发消息必带 sender_chat
    message = _message(sender_chat_id=None, is_automatic_forward=True)

    assert await antispam.check_and_handle_channel_as_sender(message, bot) is True
    repo_get.assert_not_awaited()
    message.delete.assert_not_awaited()


async def test_service_account_fast_path(mocker) -> None:
    """from 为 Telegram 系统账号（777000）→ True，不查 DB"""
    repo_get, _, bot = _stub(mocker)
    message = _message(sender_chat_id=LINKED_CHANNEL_ID, from_user_id=777000)

    assert await antispam.check_and_handle_channel_as_sender(message, bot) is True
    repo_get.assert_not_awaited()
    message.delete.assert_not_awaited()


async def test_linked_channel_identity_is_allowed(mocker) -> None:
    """sender_chat 即本群关联频道 → True 放行，走共享缓存函数，不内联 get_chat"""
    _, linked_lookup, bot = _stub(mocker, linked=LINKED_CHANNEL_ID)
    message = _message(sender_chat_id=LINKED_CHANNEL_ID)

    assert await antispam.check_and_handle_channel_as_sender(message, bot) is True
    linked_lookup.assert_awaited_once_with(message, bot)
    bot.get_chat.assert_not_awaited()
    message.delete.assert_not_awaited()


# ===== 群开关 =====


async def test_disabled_group_returns_false(mocker) -> None:
    """群关闭反频道马甲 → False（回归正常管线），不删除、不查关联频道"""
    _, linked_lookup, bot = _stub(mocker, enabled=False)
    message = _message()

    assert await antispam.check_and_handle_channel_as_sender(message, bot) is False
    linked_lookup.assert_not_awaited()
    message.delete.assert_not_awaited()


# ===== 命中 =====


async def test_other_channel_is_deleted_without_warning(mocker) -> None:
    """非关联频道身份 → 删除 + 提示，不调用 warn_user（from 是假用户 Channel_Bot）"""
    _, _, bot = _stub(mocker, linked=LINKED_CHANNEL_ID)
    message = _message(sender_chat_id=OTHER_CHANNEL_ID)

    assert await antispam.check_and_handle_channel_as_sender(message, bot) is True
    message.delete.assert_awaited_once()
    message.answer.assert_awaited_once()
    antispam.ModerationService.warn_user.assert_not_awaited()
    antispam.auto_delete_message.assert_awaited_once_with(message.answer.return_value, delay=30)


async def test_linked_lookup_failure_fails_punished(mocker) -> None:
    """关联频道查询失败（返回 None）→ 按无关联频道处理，仍删除（fail-punish）"""
    _, _, bot = _stub(mocker, linked=None, linked_error=True)
    message = _message(sender_chat_id=LINKED_CHANNEL_ID)

    assert await antispam.check_and_handle_channel_as_sender(message, bot) is True
    message.delete.assert_awaited_once()
