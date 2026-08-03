"""cmd_help i18n 测试（3c4）。

验证 /help <command> 走 catalog（admin.help.detail.<command>.message），
未知命令走 not_found.message（command 经 escape_html），无参走 overview。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit


def _localizer() -> MagicMock:
    localizer = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}:{kw}>" if kw else f"<{key}>"

    localizer.t.side_effect = fake_t
    return localizer


def _message(text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


async def test_help_known_command_uses_catalog() -> None:
    """/help <已知命令> → admin.help.detail.<command>.message。"""
    localizer = _localizer()
    message = _message("/help settimeout")

    await handler.cmd_help(message, localizer)

    localizer.t.assert_called_once_with("admin.help.detail.settimeout.message")


async def test_help_strips_slash_prefix() -> None:
    """/help /kick（带 / 前缀）→ detail.kick.message（lstrip /）。"""
    localizer = _localizer()
    message = _message("/help /kick")

    await handler.cmd_help(message, localizer)

    localizer.t.assert_called_once_with("admin.help.detail.kick.message")


async def test_help_unknown_command_not_found() -> None:
    """/help <未知命令> → not_found.message(command=...)。"""
    localizer = _localizer()
    message = _message("/help foobar")

    await handler.cmd_help(message, localizer)

    localizer.t.assert_called_once_with("admin.help.not_found.message", command="foobar")


async def test_help_command_html_escaped() -> None:
    """command 含 HTML 字符 → escape_html 后传入（防注入）。"""
    localizer = _localizer()
    message = _message("/help <script>")

    await handler.cmd_help(message, localizer)

    call = localizer.t.call_args
    assert call.args == ("admin.help.not_found.message",)
    assert call.kwargs["command"] == "&lt;script&gt;"


async def test_help_no_args_calls_overview(mocker) -> None:
    """/help 无参 → show_command_overview（不查 detail catalog）。"""
    localizer = _localizer()
    overview = mocker.patch.object(handler, "show_command_overview", new=AsyncMock())
    message = _message("/help")

    await handler.cmd_help(message, localizer)

    overview.assert_awaited_once_with(message)
    localizer.t.assert_not_called()


def test_help_commands_match_catalog_detail_keys() -> None:
    """不变量：_HELP_COMMANDS 与三语 admin.help.detail.*.message 精确对应（防漂移）。"""
    import json

    for locale in ("zh-Hans", "zh-Hant", "en"):
        with open(f"locales/{locale}.json", encoding="utf-8") as f:
            catalog = json.load(f)
        detail_keys = {
            k.removeprefix("admin.help.detail.").removesuffix(".message")
            for k in catalog
            if k.startswith("admin.help.detail.") and k.endswith(".message")
        }
        assert detail_keys == handler._HELP_COMMANDS, (
            f"{locale}: catalog detail keys != _HELP_COMMANDS\n"
            f"  catalog only: {detail_keys - handler._HELP_COMMANDS}\n"
            f"  whitelist only: {handler._HELP_COMMANDS - detail_keys}"
        )
