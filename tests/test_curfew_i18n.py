"""curfew i18n 契约测试。

覆盖：
- ``_parse_curfew_schedule`` 各 validation code 稳定 + 成功解析（含默认时区/分钟可选）
- ``cmd_curfew`` handler 各分支走 catalog（group_only/permission_denied/status.disabled/
  status.enabled 占位符/off/启用成功/validation code 渲染）
- ``CurfewScheduler._check_group`` 每次按群解析 locale（不缓存），entered/exited 用 localizer.t
- catalog 三语 parity（curfew.* key 集合 + 占位符对等）
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import curfew as curfew_module
from src.bot.handlers.curfew import _parse_curfew_schedule
from src.services.curfew import CurfewService
from src.services.curfew_scheduler import CurfewScheduler

pytestmark = pytest.mark.unit

CHAT_ID = -100123


# ==================== _parse_curfew_schedule validation code ====================


def test_parse_missing_schedule() -> None:
    """参数不足 → missing_schedule。"""
    assert _parse_curfew_schedule(["23:00"]) == (None, "missing_schedule")


def test_parse_start_format_invalid() -> None:
    assert _parse_curfew_schedule(["abc", "7:00"]) == (None, "start_format")


def test_parse_end_format_invalid() -> None:
    assert _parse_curfew_schedule(["23:00", "xyz"]) == (None, "end_format")


def test_parse_timezone_format_invalid() -> None:
    assert _parse_curfew_schedule(["23:00", "7:00", "abc"]) == (None, "timezone_format")


def test_parse_start_range_invalid() -> None:
    assert _parse_curfew_schedule(["25:00", "7:00"]) == (None, "start_range")


def test_parse_end_range_invalid() -> None:
    assert _parse_curfew_schedule(["23:00", "25:00"]) == (None, "end_range")


def test_parse_timezone_range_invalid() -> None:
    assert _parse_curfew_schedule(["23:00", "7:00", "99"]) == (None, "timezone_range")


def test_parse_success_full_with_timezone() -> None:
    assert _parse_curfew_schedule(["23:00", "7:00", "+9"]) == ((23, 0, 7, 0, 9), None)


def test_parse_success_default_timezone_plus_8() -> None:
    """未指定时区 → 默认 +8。"""
    assert _parse_curfew_schedule(["23", "7"]) == ((23, 0, 7, 0, 8), None)


def test_parse_success_minutes_optional() -> None:
    assert _parse_curfew_schedule(["23:30", "7:45"]) == ((23, 30, 7, 45, 8), None)


# ==================== cmd_curfew handler 渲染 ====================


def _message(chat_type: str = "group", text: str = "/curfew") -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = CHAT_ID
    message.chat.title = "Test"
    message.text = text
    message.from_user = MagicMock(id=42)
    message.answer = AsyncMock(return_value=MagicMock())
    return message


def _group(
    *,
    enabled: bool = False,
    start: tuple[int, int] | None = None,
    end: tuple[int, int] | None = None,
    tz: int = 8,
) -> SimpleNamespace:
    sh, sm = start if start else (None, None)
    eh, em = end if end else (None, None)
    return SimpleNamespace(
        curfew_enabled=enabled,
        curfew_start_hour=sh,
        curfew_start_minute=sm,
        curfew_end_hour=eh,
        curfew_end_minute=em,
        curfew_timezone_offset=tz,
    )


def _patch(
    mocker,
    *,
    is_admin: bool = True,
    group: SimpleNamespace | None = None,
    current_group: SimpleNamespace | None = None,
    is_in_curfew: bool = False,
) -> MagicMock:
    mocker.patch.object(
        curfew_module, "check_admin_permission", new=AsyncMock(return_value=is_admin)
    )
    mocker.patch.object(curfew_module, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(
        curfew_module.GroupRepository, "get_or_create", new=AsyncMock(return_value=group)
    )
    mocker.patch.object(
        curfew_module.GroupRepository, "get", new=AsyncMock(return_value=current_group)
    )
    mocker.patch.object(curfew_module.GroupRepository, "update_curfew_settings", new=AsyncMock())
    # is_in_curfew 是同步 staticmethod
    mocker.patch.object(CurfewService, "is_in_curfew", return_value=is_in_curfew)
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}>" if not kw else f"<{key}:{kw}>"
    return localizer


async def test_private_chat_uses_group_only(mocker) -> None:
    localizer = _patch(mocker)
    await curfew_module.cmd_curfew(_message(chat_type="private"), AsyncMock(), localizer)
    localizer.t.assert_called_once_with("common.error.group_only")


async def test_non_admin_uses_permission_denied(mocker) -> None:
    localizer = _patch(mocker, is_admin=False)
    await curfew_module.cmd_curfew(_message(), AsyncMock(), localizer)
    localizer.t.assert_called_once_with("common.error.permission_denied")


async def test_status_disabled_when_unconfigured(mocker) -> None:
    localizer = _patch(mocker, group=_group(enabled=False))
    await curfew_module.cmd_curfew(_message(text="/curfew"), AsyncMock(), localizer)
    localizer.t.assert_called_once_with("curfew.status.disabled.message")


async def test_status_enabled_renders_formatted_placeholders(mocker) -> None:
    """已启用状态 → status.enabled.message，时间格式化为字符串占位符。"""
    group = _group(enabled=True, start=(23, 0), end=(7, 0), tz=8)
    localizer = _patch(mocker, group=group, is_in_curfew=True)
    await curfew_module.cmd_curfew(_message(text="/curfew"), AsyncMock(), localizer)
    localizer.t.assert_any_call(
        "curfew.status.enabled.message",
        start_time="23:00",
        end_time="07:00",
        timezone="+8",
        state="<curfew.state.active.label>",
        rules="<curfew.rules.summary.message>",
    )


async def test_off_command_uses_disabled_message(mocker) -> None:
    localizer = _patch(mocker, group=_group(enabled=True, start=(23, 0), end=(7, 0)))
    await curfew_module.cmd_curfew(_message(text="/curfew off"), AsyncMock(), localizer)
    localizer.t.assert_called_once_with("curfew.command.disabled.message")
    curfew_module.GroupRepository.update_curfew_settings.assert_awaited_once_with(
        CHAT_ID, enabled=False
    )


async def test_enable_success_renders_command_enabled(mocker) -> None:
    """启用成功 → command.enabled.message + update_curfew_settings 写入解析值。"""
    current = _group(enabled=True, start=(23, 30), end=(7, 45), tz=9)
    localizer = _patch(mocker, group=_group(), current_group=current, is_in_curfew=False)
    await curfew_module.cmd_curfew(_message(text="/curfew 23:30 7:45 +9"), AsyncMock(), localizer)
    localizer.t.assert_any_call(
        "curfew.command.enabled.message",
        start_time="23:30",
        end_time="07:45",
        timezone="+9",
        state="<curfew.state.inactive.label>",
        rules="<curfew.rules.summary.message>",
    )
    curfew_module.GroupRepository.update_curfew_settings.assert_awaited_once_with(
        CHAT_ID,
        enabled=True,
        start_hour=23,
        start_minute=30,
        end_hour=7,
        end_minute=45,
        timezone_offset=9,
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("/curfew 23", "missing_schedule"),
        ("/curfew abc 7:00", "start_format"),
        ("/curfew 23:00 xyz", "end_format"),
        ("/curfew 23:00 7:00 abc", "timezone_format"),
        ("/curfew 25:00 7:00", "start_range"),
        ("/curfew 23:00 25:00", "end_range"),
        ("/curfew 23:00 7:00 99", "timezone_range"),
    ],
)
async def test_validation_code_rendered(mocker, text: str, code: str) -> None:
    """各 validation code → curfew.error.<code>.message。"""
    localizer = _patch(mocker, group=_group())
    await curfew_module.cmd_curfew(_message(text=text), AsyncMock(), localizer)
    localizer.t.assert_called_once_with(f"curfew.error.{code}.message")


# ==================== CurfewScheduler locale 解析 ====================


def _scheduler_setup(
    *, locale: str = "en"
) -> tuple[CurfewScheduler, MagicMock, MagicMock, MagicMock]:
    """构造 scheduler + 已绑定的 resolver/translator/localizer mock。"""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    resolver = MagicMock()
    resolver.for_group = AsyncMock(return_value=locale)
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}>" if not kw else f"<{key}:{kw}>"
    translator = MagicMock()
    translator.for_locale = MagicMock(return_value=localizer)
    scheduler = CurfewScheduler(bot, resolver, translator)
    return scheduler, resolver, translator, localizer


async def test_scheduler_entered_resolves_locale_per_group(mocker) -> None:
    """进入宵禁 → 每次 for_group 解析 locale，entered 通知用 localizer.t。"""
    scheduler, resolver, translator, localizer = _scheduler_setup(locale="en")
    group = _group(enabled=True, start=(23, 0), end=(7, 0), tz=8)
    group.id = CHAT_ID
    mocker.patch.object(CurfewService, "is_in_curfew", return_value=True)
    mocker.patch.object(
        CurfewService, "track_curfew_state", new=AsyncMock(return_value=(True, False))
    )

    await scheduler._check_group(group)

    resolver.for_group.assert_awaited_once_with(CHAT_ID)
    translator.for_locale.assert_called_once_with("en")
    localizer.t.assert_any_call(
        "curfew.scheduler.entered.group.message",
        start_time="23:00",
        end_time="07:00",
        timezone="+8",
    )


async def test_scheduler_exited_uses_localizer(mocker) -> None:
    scheduler, _resolver, _translator, localizer = _scheduler_setup()
    group = _group(enabled=True, start=(23, 0), end=(7, 0), tz=8)
    group.id = CHAT_ID
    mocker.patch.object(CurfewService, "is_in_curfew", return_value=False)
    mocker.patch.object(
        CurfewService, "track_curfew_state", new=AsyncMock(return_value=(False, True))
    )

    await scheduler._check_group(group)

    localizer.t.assert_called_once_with("curfew.scheduler.exited.group.message")


async def test_scheduler_does_not_cache_localizer_across_groups(mocker) -> None:
    """两次 _check_group 都重新 for_group + for_locale（不缓存到实例）。"""
    scheduler, resolver, translator, _localizer = _scheduler_setup()
    group = _group(enabled=True, start=(23, 0), end=(7, 0), tz=8)
    group.id = CHAT_ID
    mocker.patch.object(CurfewService, "is_in_curfew", return_value=False)
    mocker.patch.object(
        CurfewService, "track_curfew_state", new=AsyncMock(return_value=(False, False))
    )

    await scheduler._check_group(group)
    await scheduler._check_group(group)

    # 每次都解析（即便无 entered/exited 也解析 locale，保证多群多语言互不污染）
    assert resolver.for_group.await_count == 2
    assert translator.for_locale.call_count == 2


# ==================== catalog 三语 parity ====================


def _catalogs() -> dict[str, dict[str, str]]:
    root = Path(__file__).resolve().parents[1]
    return {
        loc: json.loads((root / "locales" / f"{loc}.json").read_text(encoding="utf-8"))
        for loc in ("zh-Hans", "zh-Hant", "en")
    }


def test_curfew_keys_three_locale_parity() -> None:
    cats = _catalogs()
    curfew_keys = {loc: {k for k in cats[loc] if k.startswith("curfew.")} for loc in cats}
    assert curfew_keys["zh-Hans"] == curfew_keys["zh-Hant"] == curfew_keys["en"]
    # 16 个 curfew.* key（common.error.group_only 属 common.* 不计）
    assert len(curfew_keys["zh-Hans"]) == 16
    # common.error.group_only 三语都有
    for loc in cats:
        assert "common.error.group_only" in cats[loc]


def test_curfew_placeholders_three_locale_parity() -> None:
    """含占位符的 curfew key 三语占位符集合一致。"""
    cats = _catalogs()

    def placeholders(s: str) -> set[str]:
        return set(re.findall(r"\{(\w+)\}", s))

    keys_with_ph = [
        k for k in cats["zh-Hans"] if k.startswith("curfew.") and placeholders(cats["zh-Hans"][k])
    ]
    assert keys_with_ph, "应有含占位符的 curfew key"
    for k in keys_with_ph:
        ph = {loc: placeholders(cats[loc][k]) for loc in cats}
        assert ph["zh-Hans"] == ph["zh-Hant"] == ph["en"], f"占位符不一致: {k}: {ph}"
