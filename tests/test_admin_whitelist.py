"""/whitelist i18n 测试（3c5）+ 移出白名单退群（M1）。

验证 /whitelist 及 3 个子函数（list/add/remove）走 catalog，群组显示标识
（title escape 或 chat_id）正确传入占位符。含 M1：remove 成功后主动
bot.leave_chat；DB 提交与退群失败隔离。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

SUPER_ADMIN = 1
NON_ADMIN = 999


def _message(text: str = "/whitelist", user_id: int = SUPER_ADMIN) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.from_user = MagicMock(id=user_id)
    message.answer = AsyncMock()
    return message


def _bot() -> MagicMock:
    """mock Bot：leave_chat 为 AsyncMock（M1 remove 成功后调用）。"""
    bot = MagicMock()
    bot.leave_chat = AsyncMock(return_value=True)
    return bot


def _group(gid: int = -100, title: str | None = "Test", whitelisted: bool = True) -> MagicMock:
    g = MagicMock()
    g.id = gid
    g.title = title
    g.is_whitelisted = whitelisted
    return g


def _localizer() -> MagicMock:
    localizer = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}:{kw}>" if kw else f"<{key}>"

    localizer.t.side_effect = fake_t
    return localizer


# ===== cmd_whitelist 总入口 =====
async def test_non_super_admin_denied(mocker) -> None:
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    localizer = _localizer()
    message = _message(user_id=NON_ADMIN)

    await handler.cmd_whitelist(message, _bot(), localizer)

    localizer.t.assert_called_once_with("admin.whitelist.error.permission_denied.message")


async def test_unknown_subcommand(mocker) -> None:
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    localizer = _localizer()
    message = _message("/whitelist foobar")

    await handler.cmd_whitelist(message, _bot(), localizer)

    localizer.t.assert_called_once_with("admin.whitelist.error.unknown_subcommand.message")


# ===== _list_whitelist =====
async def test_list_empty(mocker) -> None:
    mocker.patch.object(
        handler.GroupRepository, "get_whitelisted_groups", new=AsyncMock(return_value=[])
    )
    localizer = _localizer()
    message = _message()

    await handler._list_whitelist(message, localizer)

    localizer.t.assert_called_once_with("admin.whitelist.list.empty.message")


async def test_list_passes_count_and_rows(mocker) -> None:
    g1 = _group(gid=-100, title="Group A")
    g2 = _group(gid=-200, title="Group B")
    mocker.patch.object(
        handler.GroupRepository, "get_whitelisted_groups", new=AsyncMock(return_value=[g1, g2])
    )
    localizer = _localizer()
    message = _message()

    await handler._list_whitelist(message, localizer)

    # 第一次 t 是 header（count=2），后续 2 个 row（各 3 占位符 index/title/chat_id）
    first = localizer.t.call_args_list[0]
    assert first.args == ("admin.whitelist.list.header.message",)
    assert first.kwargs == {"count": 2}
    # row 调用应出现 2 次
    row_calls = [
        c for c in localizer.t.call_args_list if c.args == ("admin.whitelist.list.row.message",)
    ]
    assert len(row_calls) == 2
    assert row_calls[0].kwargs == {"index": 1, "title": "Group A", "chat_id": -100}


async def test_list_title_none_uses_unknown_label(mocker) -> None:
    g = _group(gid=-100, title=None)
    mocker.patch.object(
        handler.GroupRepository, "get_whitelisted_groups", new=AsyncMock(return_value=[g])
    )
    localizer = _localizer()
    message = _message()

    await handler._list_whitelist(message, localizer)

    # row 的 title 占位符应为 unknown_group label
    row_call = next(
        c for c in localizer.t.call_args_list if c.args == ("admin.whitelist.list.row.message",)
    )
    assert row_call.kwargs["title"] == "<admin.common.unknown_group.label>"


# ===== _add_whitelist =====
async def test_add_missing_arg(mocker) -> None:
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    localizer = _localizer()
    message = _message("/whitelist add")

    await handler._add_whitelist(message, ["/whitelist", "add"], localizer)

    localizer.t.assert_called_once_with("admin.whitelist.add.error.missing_arg.message")


async def test_add_already_whitelisted(mocker) -> None:
    g = _group(gid=-100, title="Test", whitelisted=True)
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=g))
    update = mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    localizer = _localizer()
    message = _message("/whitelist add -100")

    await handler._add_whitelist(message, ["/whitelist", "add", "-100"], localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.whitelist.add.already_in.message",)
    assert last.kwargs["group"] == "Test"  # title escape（无特殊字符）
    update.assert_not_awaited()


async def test_add_invalid_id_valueerror(mocker) -> None:
    mocker.patch.object(
        handler.GroupRepository, "get_or_create", new=AsyncMock(side_effect=ValueError)
    )
    localizer = _localizer()
    message = _message("/whitelist add abc")

    await handler._add_whitelist(message, ["/whitelist", "add", "abc"], localizer)

    localizer.t.assert_called_once_with("admin.whitelist.add.error.invalid_id.message")


async def test_add_success(mocker) -> None:
    g = _group(gid=-100, title="NewGroup", whitelisted=False)
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=g))
    mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    localizer = _localizer()
    message = _message("/whitelist add -100 NewGroup")

    await handler._add_whitelist(message, ["/whitelist", "add", "-100", "NewGroup"], localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.whitelist.add.saved.message",)
    assert last.kwargs["group"] == "NewGroup"
    handler.GroupRepository.update_whitelist.assert_awaited_once_with(-100, True)


async def test_add_html_title_escaped(mocker) -> None:
    """title 含 HTML 字符 → escape 后传入 {group} 占位符。"""
    g = _group(gid=-100, title="<x>&Co", whitelisted=False)
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=g))
    mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    localizer = _localizer()
    message = _message("/whitelist add -100 <x>&Co")

    await handler._add_whitelist(message, ["/whitelist", "add", "-100", "<x>&Co"], localizer)

    last = localizer.t.call_args
    assert last.kwargs["group"] == "&lt;x&gt;&amp;Co"


async def test_add_title_none_uses_chat_id(mocker) -> None:
    """title=None → {group} 用 chat_id（数字，无需 escape）。"""
    g = _group(gid=-1001234567890, title=None, whitelisted=False)
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=g))
    mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    localizer = _localizer()
    message = _message("/whitelist add -1001234567890")

    await handler._add_whitelist(message, ["/whitelist", "add", "-1001234567890"], localizer)

    last = localizer.t.call_args
    assert last.kwargs["group"] == -1001234567890


# ===== _remove_whitelist（含 M1：成功后退群）=====
async def test_remove_not_found(mocker) -> None:
    mocker.patch.object(handler.GroupRepository, "get_by_id", new=AsyncMock(return_value=None))
    bot = _bot()
    localizer = _localizer()
    message = _message("/whitelist remove -999")

    await handler._remove_whitelist(message, bot, ["/whitelist", "remove", "-999"], localizer)

    localizer.t.assert_called_once_with(
        "admin.whitelist.remove.error.not_found.message", chat_id=-999
    )
    bot.leave_chat.assert_not_awaited()


async def test_remove_not_in_whitelist(mocker) -> None:
    g = _group(gid=-100, title="Test", whitelisted=False)  # 不在白名单
    mocker.patch.object(handler.GroupRepository, "get_by_id", new=AsyncMock(return_value=g))
    update = mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    bot = _bot()
    localizer = _localizer()
    message = _message("/whitelist remove -100")

    await handler._remove_whitelist(message, bot, ["/whitelist", "remove", "-100"], localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.whitelist.remove.not_in.message",)
    assert last.kwargs["group"] == "Test"
    update.assert_not_awaited()
    bot.leave_chat.assert_not_awaited()


async def test_remove_success_leaves_chat(mocker) -> None:
    """M1：remove 成功 → update_whitelist(-100, False) 后主动 leave_chat(-100)。"""
    g = _group(gid=-100, title="Test", whitelisted=True)
    mocker.patch.object(handler.GroupRepository, "get_by_id", new=AsyncMock(return_value=g))
    mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    bot = _bot()
    localizer = _localizer()
    message = _message("/whitelist remove -100")

    await handler._remove_whitelist(message, bot, ["/whitelist", "remove", "-100"], localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.whitelist.remove.saved.message",)
    assert last.kwargs["group"] == "Test"
    handler.GroupRepository.update_whitelist.assert_awaited_once_with(-100, False)
    bot.leave_chat.assert_awaited_once_with(-100)


async def test_remove_success_leave_failure_does_not_fail(mocker) -> None:
    """M1：leave_chat 抛异常不应把已提交的 DB 变更报告为失败（saved 文案仍发出）。"""
    g = _group(gid=-100, title="Test", whitelisted=True)
    mocker.patch.object(handler.GroupRepository, "get_by_id", new=AsyncMock(return_value=g))
    mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    bot = MagicMock()
    bot.leave_chat = AsyncMock(side_effect=RuntimeError("already left"))
    localizer = _localizer()
    message = _message("/whitelist remove -100")

    await handler._remove_whitelist(message, bot, ["/whitelist", "remove", "-100"], localizer)

    # DB 变更成功 + saved 文案照常发出，不进入 failed 分支
    handler.GroupRepository.update_whitelist.assert_awaited_once_with(-100, False)
    assert localizer.t.call_args.args == ("admin.whitelist.remove.saved.message",)


async def test_remove_invalid_id_valueerror(mocker) -> None:
    # int(args[2]) 抛 ValueError
    bot = _bot()
    localizer = _localizer()
    message = _message("/whitelist remove abc")

    await handler._remove_whitelist(message, bot, ["/whitelist", "remove", "abc"], localizer)

    localizer.t.assert_called_once_with("admin.whitelist.remove.error.invalid_id.message")
    bot.leave_chat.assert_not_awaited()


async def test_remove_missing_arg(mocker) -> None:
    """参数不足（len != 3）→ missing_arg key。"""
    bot = _bot()
    localizer = _localizer()
    message = _message("/whitelist remove")

    await handler._remove_whitelist(message, bot, ["/whitelist", "remove"], localizer)

    localizer.t.assert_called_once_with("admin.whitelist.remove.error.missing_arg.message")
    bot.leave_chat.assert_not_awaited()


async def test_remove_html_title_escaped(mocker) -> None:
    """remove 成功时 title HTML 字符 → escape 后传入 {group}；退群 best-effort 不影响文案。"""
    g = _group(gid=-100, title="<x>&Co", whitelisted=True)
    mocker.patch.object(handler.GroupRepository, "get_by_id", new=AsyncMock(return_value=g))
    mocker.patch.object(handler.GroupRepository, "update_whitelist", new=AsyncMock())
    bot = _bot()
    localizer = _localizer()
    message = _message("/whitelist remove -100")

    await handler._remove_whitelist(message, bot, ["/whitelist", "remove", "-100"], localizer)

    last = localizer.t.call_args
    assert last.kwargs["group"] == "&lt;x&gt;&amp;Co"
    bot.leave_chat.assert_awaited_once_with(-100)
