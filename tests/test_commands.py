"""命令菜单 locale 化测试（3c3）。

覆盖：
- build_commands 按 localizer 渲染 BotCommand
- build_commands 缺翻译抛 ValueError（防混合语言菜单）
- setup_fallback_commands 调 4 scope（Default/AllPrivate/AllGroup/AllAdmin）
- sync_chat_commands 私聊/群组 scope 选择 + locale 透传
- catalog 三语 command.*.description parity
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.commands import (
    DEFAULT_COMMANDS,
    GROUP_ADMIN_COMMANDS,
    GROUP_MEMBER_COMMANDS,
    PRIVATE_COMMANDS,
    CommandSpec,
    _specs,
    build_commands,
    setup_fallback_commands,
    sync_chat_commands,
)

pytestmark = pytest.mark.unit


def _real_localizer(locale: str):
    """用真实 catalog 构造 localizer（验证翻译实际存在）。"""
    from src.core.i18n.catalog import load_catalogs
    from src.core.i18n.translator import Translator

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    return Translator(catalogs, "zh-Hans").for_locale(locale)


# ===== CommandSpec / _specs =====
def test_specs_creates_command_spec_with_catalog_key() -> None:
    """_specs 批量构造，description_key 统一 command.<name>.description。"""
    specs = _specs("start", "help")
    assert specs == (
        CommandSpec(name="start", description_key="command.start.description"),
        CommandSpec(name="help", description_key="command.help.description"),
    )


def test_command_groups_contain_expected_commands() -> None:
    """4 组命令含关键命令（lang/reject 等新增项）。"""
    assert [s.name for s in DEFAULT_COMMANDS] == ["start", "help", "lang"]
    assert "lang" in [s.name for s in PRIVATE_COMMANDS]
    assert "reject" in [s.name for s in GROUP_ADMIN_COMMANDS]  # codex 发现的遗漏命令
    assert "lang" in [s.name for s in GROUP_ADMIN_COMMANDS]  # 管理员可切换群语言
    # 群成员仅基础命令
    assert [s.name for s in GROUP_MEMBER_COMMANDS] == ["help", "spam", "report"]


# ===== build_commands 渲染 =====
@pytest.mark.parametrize("locale", ["zh-Hans", "zh-Hant", "en"])
def test_build_commands_renders_all_specs_with_real_catalog(locale: str) -> None:
    """所有命令组用真实 catalog 渲染无缺翻译（防混合语言菜单）。"""
    localizer = _real_localizer(locale)
    for specs in (DEFAULT_COMMANDS, PRIVATE_COMMANDS, GROUP_MEMBER_COMMANDS, GROUP_ADMIN_COMMANDS):
        commands = build_commands(specs, localizer)
        assert len(commands) == len(specs)
        # 每条 description 非空且非裸 key（已翻译）
        for cmd, spec in zip(commands, specs, strict=True):
            assert cmd.command == spec.name
            assert cmd.description != spec.description_key
            assert cmd.description  # 非空


def test_build_commands_missing_translation_raises() -> None:
    """缺翻译 key → ValueError（防混合语言菜单）。"""
    localizer = MagicMock()
    localizer.locale = "zh-Hans"
    # localizer.t 返回 key 本身（模拟非严格模式缺 key）
    localizer.t = MagicMock(return_value="command.nonexistent.description")
    specs = _specs("nonexistent")

    with pytest.raises(ValueError, match="缺少翻译"):
        build_commands(specs, localizer)


# ===== setup_fallback_commands 4 scope =====
async def test_setup_fallback_commands_sets_four_scopes() -> None:
    """启动时设 Default/AllPrivate/AllGroup/AllAdmin 4 个 scope。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    localizer = _real_localizer("en")

    await setup_fallback_commands(bot, localizer)

    assert bot.set_my_commands.await_count == 4
    # 验证 scope 类型
    scopes = [call.kwargs["scope"] for call in bot.set_my_commands.await_args_list]
    scope_types = {type(s).__name__ for s in scopes}
    assert scope_types == {
        "BotCommandScopeDefault",
        "BotCommandScopeAllPrivateChats",
        "BotCommandScopeAllGroupChats",
        "BotCommandScopeAllChatAdministrators",
    }


async def test_setup_fallback_commands_failure_does_not_block_others() -> None:
    """某 scope 失败仅记日志，不阻断其他 scope（决策5：不回滚）。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock(side_effect=RuntimeError("net error"))
    localizer = _real_localizer("zh-Hans")

    # 不抛异常（_set_scope_commands 兜底）
    await setup_fallback_commands(bot, localizer)
    assert bot.set_my_commands.await_count == 4  # 4 scope 都尝试


# ===== sync_chat_commands scope 选择 =====
async def test_sync_chat_commands_private_uses_chat_scope() -> None:
    """私聊 → BotCommandScopeChat(chat_id) + PRIVATE_COMMANDS。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    localizer = _real_localizer("en")

    await sync_chat_commands(bot, localizer, chat_id=12345, is_group=False)

    assert bot.set_my_commands.await_count == 1
    call = bot.set_my_commands.await_args_list[0]
    scope = call.kwargs["scope"]
    assert type(scope).__name__ == "BotCommandScopeChat"
    assert scope.chat_id == 12345
    # PRIVATE_COMMANDS 命令数
    assert len(call.kwargs["commands"]) == len(PRIVATE_COMMANDS)


async def test_sync_chat_commands_group_sets_member_and_admin_scopes() -> None:
    """群组 → BotCommandScopeChat(成员) + BotCommandScopeChatAdministrators(管理员)。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    localizer = _real_localizer("zh-Hant")

    await sync_chat_commands(bot, localizer, chat_id=-100123, is_group=True)

    assert bot.set_my_commands.await_count == 2
    scopes = [call.kwargs["scope"] for call in bot.set_my_commands.await_args_list]
    scope_types = {type(s).__name__ for s in scopes}
    assert scope_types == {"BotCommandScopeChat", "BotCommandScopeChatAdministrators"}
    # 两 scope 都绑定同一 chat_id
    for s in scopes:
        assert s.chat_id == -100123
    # 第一个是成员命令，第二个是管理员命令
    first_cmds = bot.set_my_commands.await_args_list[0].kwargs["commands"]
    second_cmds = bot.set_my_commands.await_args_list[1].kwargs["commands"]
    assert len(first_cmds) == len(GROUP_MEMBER_COMMANDS)
    assert len(second_cmds) == len(GROUP_ADMIN_COMMANDS)


async def test_sync_chat_commands_passes_locale_translations() -> None:
    """sync 时命令描述按 locale 翻译（验证非裸 key）。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    localizer = _real_localizer("en")

    await sync_chat_commands(bot, localizer, chat_id=1, is_group=False)

    commands = bot.set_my_commands.await_args_list[0].kwargs["commands"]
    for cmd in commands:
        # 英文翻译不应是裸 key
        assert not cmd.description.startswith("command.")


# ===== catalog 三语 parity（所有 CommandSpec 引用的 key 都存在）=====
def test_all_command_description_keys_exist_in_three_locales() -> None:
    """DEFAULT/PRIVATE/GROUP_MEMBER/GROUP_ADMIN 引用的 key 三语 catalog 均存在。"""
    all_specs = set(DEFAULT_COMMANDS) | set(PRIVATE_COMMANDS)
    all_specs |= set(GROUP_MEMBER_COMMANDS) | set(GROUP_ADMIN_COMMANDS)
    expected_keys = {spec.description_key for spec in all_specs}

    root = Path(__file__).resolve().parents[1]
    for locale in ("zh-Hans", "zh-Hant", "en"):
        import json

        catalog = json.loads((root / "locales" / f"{locale}.json").read_text("utf-8"))
        missing = expected_keys - set(catalog)
        assert not missing, f"{locale} 缺命令翻译: {missing}"
