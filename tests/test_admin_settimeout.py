"""cmd_set_timeout i18n 测试（3c2-1）。

验证 /settimeout 的 7 个文案分支走 catalog（localizer.t 以正确 key 调用），
而非硬编码中文。localizer 由 LocaleMiddleware 注入，测试直接传 mock。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _message(chat_type: str = "group", text: str = "/settimeout 120") -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = CHAT_ID
    message.chat.title = "Test"
    message.text = text
    message.from_user = MagicMock(id=42)
    message.answer = AsyncMock(return_value=MagicMock())
    message.delete = AsyncMock()
    return message


def _patch(mocker, is_admin: bool = True) -> MagicMock:
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=is_admin))
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock())
    mocker.patch.object(handler.GroupRepository, "update_verification_timeout", new=AsyncMock())
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}>" if not kw else f"<{key}:{kw}>"
    return localizer


async def test_private_chat_rejected(mocker) -> None:
    """私聊 → group_only key（直接返回，不 delete）。"""
    localizer = _patch(mocker)
    message = _message(chat_type="private")

    await handler.cmd_set_timeout(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.settimeout.error.group_only.message")
    message.answer.assert_awaited_once()
    message.delete.assert_not_awaited()


async def test_non_admin_rejected(mocker) -> None:
    """非管理员 → admin_only key。"""
    localizer = _patch(mocker, is_admin=False)
    message = _message()

    await handler.cmd_set_timeout(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.settimeout.error.admin_only.message")
    handler.check_admin_permission.assert_awaited_once()


async def test_success_passes_timeout_placeholder(mocker) -> None:
    """合法输入 → saved key，timeout 作为占位符传入。"""
    localizer = _patch(mocker)
    message = _message(text="/settimeout 120")

    await handler.cmd_set_timeout(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.settimeout.result.saved.message", timeout=120)
    handler.GroupRepository.update_verification_timeout.assert_awaited_once_with(CHAT_ID, 120)


async def test_missing_arg_validation(mocker) -> None:
    """无参数 → missing_arg key。"""
    localizer = _patch(mocker)
    message = _message(text="/settimeout")

    await handler.cmd_set_timeout(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.settimeout.validation.missing_arg.message")


async def test_not_integer_validation(mocker) -> None:
    """非数字 → not_integer key。"""
    localizer = _patch(mocker)
    message = _message(text="/settimeout abc")

    await handler.cmd_set_timeout(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.settimeout.validation.not_integer.message")


async def test_out_of_range_validation(mocker) -> None:
    """超范围 → out_of_range key。"""
    localizer = _patch(mocker)
    message = _message(text="/settimeout 999")

    await handler.cmd_set_timeout(message, AsyncMock(), localizer)

    localizer.t.assert_called_once_with("admin.settimeout.validation.out_of_range.message")


async def test_update_failure_returns_failed_key(mocker) -> None:
    """update_verification_timeout 抛异常 → failed key（捕获后通知，不传播）。"""
    localizer = _patch(mocker)
    # 给一个合法输入以越过所有验证，在写库时抛异常
    mocker.patch.object(
        handler.GroupRepository,
        "update_verification_timeout",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )
    message = _message(text="/settimeout 120")

    await handler.cmd_set_timeout(message, AsyncMock(), localizer)

    # 验证未触发 saved，触发 failed（跳过中间验证 key 的断言，只验最终 key）
    assert message.answer.await_args.args[0] == "<admin.settimeout.error.failed.message>"
