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
    rehydrate_custom_locale_commands,
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


# ===== rehydrate_custom_locale_commands（P1：启动恢复已保存 locale）=====
async def test_rehydrate_syncs_groups_and_users_with_custom_locale(mocker) -> None:
    """非默认 locale 的群/用户 → 启动时逐个 sync 命令菜单。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    translator = MagicMock()
    translator.for_locale = MagicMock(side_effect=lambda loc: _real_localizer(loc))

    mocker.patch(
        "src.repositories.group_repo.GroupRepository.get_groups_with_custom_locale",
        new=AsyncMock(return_value=[(-100, "en"), (-200, "zh-Hant")]),
    )
    mocker.patch(
        "src.repositories.user_settings_repo.UserSettingsRepository.get_users_with_custom_locale",
        new=AsyncMock(return_value=[(12345, "en")]),
    )

    await rehydrate_custom_locale_commands(bot, translator, "zh-Hans")

    # 2 群（每群 2 scope：成员+管理员）+ 1 用户（1 scope）= 5 次 set_my_commands
    assert bot.set_my_commands.await_count == 5
    # translator.for_locale 被群/用户的 locale 各调用
    called_locales = {call.args[0] for call in translator.for_locale.call_args_list}
    assert called_locales == {"en", "zh-Hant"}


async def test_rehydrate_skips_when_all_default_locale(mocker) -> None:
    """无非默认 locale → 不调 set_my_commands。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    translator = MagicMock()
    translator.for_locale = MagicMock(side_effect=lambda loc: _real_localizer(loc))

    mocker.patch(
        "src.repositories.group_repo.GroupRepository.get_groups_with_custom_locale",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "src.repositories.user_settings_repo.UserSettingsRepository.get_users_with_custom_locale",
        new=AsyncMock(return_value=[]),
    )

    await rehydrate_custom_locale_commands(bot, translator, "zh-Hans")

    bot.set_my_commands.assert_not_awaited()


async def test_rehydrate_db_failure_does_not_block(mocker) -> None:
    """DB 查询失败仅记日志，不阻断（另一类查询仍尝试）。"""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    translator = MagicMock()
    translator.for_locale = MagicMock(side_effect=lambda loc: _real_localizer(loc))

    mocker.patch(
        "src.repositories.group_repo.GroupRepository.get_groups_with_custom_locale",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )
    mocker.patch(
        "src.repositories.user_settings_repo.UserSettingsRepository.get_users_with_custom_locale",
        new=AsyncMock(return_value=[(12345, "en")]),
    )

    # 不抛异常
    await rehydrate_custom_locale_commands(bot, translator, "zh-Hans")
    # 用户查询成功 → 仍 sync 用户（1 scope）
    assert bot.set_my_commands.await_count == 1


async def test_rehydrate_skips_unsupported_locale(mocker) -> None:
    """DB 历史脏 locale（如 i18n 前写入的 zh-CN）跳过，不崩溃启动。

    rehydrate 处理 DB 已有值，translator 严格模式对不支持 locale raise；启动路径不容失败，
    跳过该 chat 保留全局兜底（用户重新 /lang 可修正）。
    """
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    translator = MagicMock()

    def for_locale_side_effect(loc: str):
        if loc not in ("zh-Hans", "zh-Hant", "en"):
            raise ValueError(f"不支持的 locale: {loc}")
        return _real_localizer(loc)

    translator.for_locale = MagicMock(side_effect=for_locale_side_effect)

    mocker.patch(
        "src.repositories.group_repo.GroupRepository.get_groups_with_custom_locale",
        new=AsyncMock(return_value=[(-100, "zh-CN"), (-200, "en")]),
    )
    mocker.patch(
        "src.repositories.user_settings_repo.UserSettingsRepository.get_users_with_custom_locale",
        new=AsyncMock(return_value=[(12345, "zh-CN"), (67890, "zh-Hant")]),
    )

    # 不抛异常（zh-CN 跳过）
    await rehydrate_custom_locale_commands(bot, translator, "zh-Hans")

    # zh-CN 群/用户跳过；en 群（2 scope：成员+管理员）+ zh-Hant 用户（1 scope）= 3 次
    assert bot.set_my_commands.await_count == 3
