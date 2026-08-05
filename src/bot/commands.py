"""Telegram 命令菜单定义、渲染与 scope 同步。

命令描述走 catalog（``command.<name>.description``），按 Bot 内 locale 渲染。
启动时设置默认 locale 的全局兜底菜单（4 scope）；/lang 写穿后按 chat_id
覆盖具体私聊/群聊的命令菜单。不传 language_code，命令语言只由 Bot 内
locale 决定（独立于 Telegram 系统语言）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
    BotCommandScopeUnion,
)
from loguru import logger

from src.core.i18n.translator import BoundLocalizer

if TYPE_CHECKING:
    from src.core.i18n.translator import Translator


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """单条命令定义。description_key 指向 catalog 翻译。"""

    name: str
    description_key: str


def _specs(*names: str) -> tuple[CommandSpec, ...]:
    """批量构造 CommandSpec，description_key 统一为 command.<name>.description。"""
    return tuple(
        CommandSpec(name=name, description_key=f"command.{name}.description") for name in names
    )


# Default scope 是兜底（无更具体 scope 覆盖时显示）。各 scope 为整表覆盖非合并，
# 故 private/admin 列表仍需显式包含 start/help/lang 等基础命令。
DEFAULT_COMMANDS = _specs("start", "help", "lang")

# 私聊命令（普通用户 + 超级管理员）
PRIVATE_COMMANDS = _specs("start", "help", "lang", "health", "stats", "whitelist")

# 群组普通成员命令（仅基础功能）
GROUP_MEMBER_COMMANDS = _specs("help", "spam", "report")

# 群组管理员命令（完整管理功能；lang 供管理员切换群语言）
GROUP_ADMIN_COMMANDS = _specs(
    "groupset",
    "setverify",
    "settimeout",
    "verifyconfig",
    "antispam",
    "antichannel",
    "activity",
    "activityskip",
    "curfew",
    "kick",
    "mute",
    "unmute",
    "ban",
    "unban",
    "warn",
    "warnings",
    "clearwarnings",
    "cleanup",
    "delbefore",
    "delafter",
    "delrange",
    "spam",
    "report",
    "notspam",
    "nospam",
    "unspam",
    "reports",
    "approve",
    "reject",
    "help",
    "lang",
)


def build_commands(specs: Sequence[CommandSpec], localizer: BoundLocalizer) -> list[BotCommand]:
    """按 localizer 渲染 BotCommand 列表。

    缺翻译时抛 ValueError（防混合语言菜单）；由调用方兜底保留旧菜单。
    """
    commands: list[BotCommand] = []
    for spec in specs:
        description = localizer.t(spec.description_key)
        # 非严格模式缺 key 返回 key 本身 → 视为缺翻译
        if description == spec.description_key:
            raise ValueError(f"命令菜单缺少翻译 [{localizer.locale}:{spec.description_key}]")
        commands.append(BotCommand(command=spec.name, description=description))
    return commands


async def _set_scope_commands(
    bot: Bot,
    localizer: BoundLocalizer,
    specs: Sequence[CommandSpec],
    scope: BotCommandScopeUnion,
    scope_name: str,
) -> None:
    """独立更新一个 scope；失败仅记日志并保留 Telegram 端旧菜单（不回滚 locale）。"""
    try:
        commands = build_commands(specs, localizer)
        await bot.set_my_commands(commands=commands, scope=scope)
    except Exception as exc:
        logger.error(f"同步命令菜单失败 [scope:{scope_name}] [locale:{localizer.locale}]: {exc}")
        return

    logger.info(
        f"已同步命令菜单 [scope:{scope_name}] [locale:{localizer.locale}] [命令数:{len(commands)}]"
    )


# type alias 用于 targets 元组类型标注（aiogram set_my_commands 接受的 scope 联合）
type _ScopeTarget = tuple[str, Sequence[CommandSpec], BotCommandScopeUnion]


async def setup_fallback_commands(bot: Bot, localizer: BoundLocalizer) -> None:
    """设置默认 locale 的全局兜底菜单（启动时调用）。"""
    targets: tuple[_ScopeTarget, ...] = (
        ("default", DEFAULT_COMMANDS, BotCommandScopeDefault()),
        ("all_private_chats", PRIVATE_COMMANDS, BotCommandScopeAllPrivateChats()),
        ("all_group_chats", GROUP_MEMBER_COMMANDS, BotCommandScopeAllGroupChats()),
        (
            "all_chat_administrators",
            GROUP_ADMIN_COMMANDS,
            BotCommandScopeAllChatAdministrators(),
        ),
    )
    for scope_name, specs, scope in targets:
        await _set_scope_commands(bot, localizer, specs, scope, scope_name)


def _safe_localizer(translator: "Translator", locale: str, context: str) -> BoundLocalizer | None:
    """启动恢复容错：无法归一的 locale 跳过，不崩溃启动。

    rehydrate 处理 DB 中已有的 locale，可能含 i18n 之前写入的 Telegram language_code
    历史值；已知别名由 translator 归一，真正未知的脏值在此跳过并保留全局兜底菜单。
    """
    try:
        return translator.for_locale(locale)
    except ValueError:
        logger.warning(f"跳过不支持的 locale [{context}] [locale:{locale}]，命令菜单保留全局兜底")
        return None


async def rehydrate_custom_locale_commands(
    bot: Bot,
    translator: "Translator",
    default_locale: str,
) -> None:
    """启动时恢复已保存的非默认 locale 命令菜单（3c3 P1）。

    首次部署本特性时，DB 里可能已有 groups.locale / user_settings.locale 的非默认值。
    仅设全局兜底会让这些 chat 仍显示默认语言菜单，直到用户重新 /lang。
    遍历非默认 locale 的群/用户，逐个 sync 命令菜单使其立即生效。
    """
    # 延迟 import 避免循环依赖（commands 在启动早期被 main 调用）
    from src.repositories.group_repo import GroupRepository
    from src.repositories.user_settings_repo import UserSettingsRepository

    try:
        groups = await GroupRepository.get_groups_with_custom_locale(default_locale)
    except Exception as exc:
        logger.error(f"读取非默认 locale 群组失败，跳过命令菜单恢复: {exc}")
        groups = []
    for chat_id, locale in groups:
        localizer = _safe_localizer(translator, locale, f"群组:{chat_id}")
        if localizer is not None:
            await sync_chat_commands(bot, localizer, chat_id=chat_id, is_group=True)

    try:
        users = await UserSettingsRepository.get_users_with_custom_locale(default_locale)
    except Exception as exc:
        logger.error(f"读取非默认 locale 用户失败，跳过命令菜单恢复: {exc}")
        users = []
    for user_id, locale in users:
        localizer = _safe_localizer(translator, locale, f"用户:{user_id}")
        if localizer is not None:
            await sync_chat_commands(bot, localizer, chat_id=user_id, is_group=False)

    if groups or users:
        logger.info(f"已恢复非默认 locale 命令菜单 [群组:{len(groups)}] [用户:{len(users)}]")


async def sync_chat_commands(
    bot: Bot,
    localizer: BoundLocalizer,
    *,
    chat_id: int,
    is_group: bool,
) -> None:
    """按 Bot 内 locale 覆盖具体私聊或群聊的命令菜单（/lang 写穿后调用）。

    - 私聊：BotCommandScopeChat(chat_id) 设 PRIVATE_COMMANDS
    - 群组：同时设 BotCommandScopeChat(成员) + BotCommandScopeChatAdministrators(管理员)
    """
    if is_group:
        targets: tuple[_ScopeTarget, ...] = (
            ("chat:" + str(chat_id), GROUP_MEMBER_COMMANDS, BotCommandScopeChat(chat_id=chat_id)),
            (
                "chat_administrators:" + str(chat_id),
                GROUP_ADMIN_COMMANDS,
                BotCommandScopeChatAdministrators(chat_id=chat_id),
            ),
        )
    else:
        targets = (
            ("chat:" + str(chat_id), PRIVATE_COMMANDS, BotCommandScopeChat(chat_id=chat_id)),
        )

    for scope_name, specs, scope in targets:
        await _set_scope_commands(bot, localizer, specs, scope, scope_name)
