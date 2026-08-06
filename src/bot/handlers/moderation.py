"""群管理命令处理器"""

import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from src.core.config import settings
from src.core.i18n import BoundLocalizer
from src.core.redis import RedisKeys, get_redis
from src.core.utils import (
    auto_delete_message,
    check_admin_permission_strict,
    check_admin_permission_strict_message,
    escape_html,
    get_chat_administrators_mention,
    parse_message_link,
    parse_message_link_with_chat,
)
from src.repositories.report_repo import ReportRepository
from src.repositories.spam_repo import SpamRepository
from src.repositories.user_repo import UserRepository
from src.services.moderation import ModerationErrorCode, ModerationService

router = Router(name="moderation")


def _render_moderation_error(
    localizer: BoundLocalizer,
    code: ModerationErrorCode,
) -> str:
    """把服务层 error code 映射为当前群组语言的用户可见错误文案。

    catalog 文案为受控纯文本（无需 escape）；调用方将其插入到全局
    HTML parse_mode 的消息中时，由 ``moderation.error.<code>.message``
    提供稳定的本地化失败原因。
    """
    return localizer.t(f"moderation.error.{code.value}.message")


# 系统警告 reason（bot 自动警告）的稳定 code → catalog key 映射。
# 历史兼容：修复前直接写入 warnings.reason 的中文也映射，避免旧记录跨 locale 泄漏。
_WARNING_REASON_CATALOG_KEYS: dict[str, str] = {
    "system:channel_impersonation": "moderation.warnings.system_reason.channel_impersonation.label",
    "使用频道马甲发言": "moderation.warnings.system_reason.channel_impersonation.label",
}


def _render_warning_reason(localizer: BoundLocalizer, reason: str | None) -> str:
    """渲染 /warnings 列表中的 reason 字段。

    已知系统警告 code（含历史中文值）按群 locale 渲染；其余视为管理员自由
    输入文本，escape 后展示；空值回落到 no_reason label。
    """

    if not reason:
        return localizer.t("moderation.warnings.no_reason.label")
    catalog_key = _WARNING_REASON_CATALOG_KEYS.get(reason)
    if catalog_key:
        return localizer.t(catalog_key)
    return escape_html(reason)


async def parse_user_from_message(message: Message, bot: Bot) -> int | None:
    """从消息中解析用户ID

    支持的格式：
    1. 回复消息
    2. 用户ID：/command 123456
    3. @提及用户（text_mention）：/command @user
    4. @username（通过本地映射 + API 验证）
    """
    # 1. 检查是否回复了某条消息
    if message.reply_to_message:
        # ✅ 修复：检查是否为用户消息（排除频道消息）
        if message.reply_to_message.from_user:
            return message.reply_to_message.from_user.id
        # 如果是频道消息，返回 None
        return None

    # 2. 检查 entities 中的 text_mention 和 mention
    if message.text and message.entities:
        for entity in message.entities:
            # text_mention: 包含完整 User 对象
            if entity.type == "text_mention" and entity.user:
                logger.debug(f"通过 text_mention 解析到用户: {entity.user.id}")
                return entity.user.id

            # ✅ mention: @username 格式（通过映射查询 + API 验证）
            if entity.type == "mention":
                # 提取 @username
                username = message.text[entity.offset : entity.offset + entity.length]
                if username.startswith("@"):
                    username = username[1:]

                # ✅ 通过全局映射 + API 实时验证
                from src.services.username_mapping import UsernameMappingService

                user_id = await UsernameMappingService.get_user_id_by_username(
                    username=username,
                    bot=bot,
                    chat_id=message.chat.id,
                )

                if user_id:
                    logger.info(f"通过 username 映射解析到用户: @{username} -> {user_id}")
                    return user_id
                else:
                    logger.debug(
                        f"未找到 @username 映射或验证失败: @{username}，"
                        "该用户可能未在群组发言、已更改 username 或已离开群组"
                    )
                    # 继续尝试其他解析方式
                    continue

    # 3. 检查命令参数中的用户ID
    if message.text:
        # ... 现有纯数字 ID 解析逻辑保持不变 ...
        if not message.text:
            return None
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip().split()[0]  # 只取第一个参数

            # 尝试解析纯数字ID
            if arg.isdigit():
                user_id = int(arg)
                # ✅ M1: 验证 Telegram 用户 ID 范围（1 到 2^63-1）
                if 1 <= user_id <= 9223372036854775807:
                    logger.debug(f"通过用户ID解析: {user_id}")
                    return user_id
                else:
                    logger.warning(f"无效的用户 ID: {user_id}")
                    return None

            # 如果是 @username 格式但没有在 entities 中找到
            if arg.startswith("@"):
                logger.warning(
                    "检测到 @username 格式但无法解析用户ID。"
                    "可能原因：用户没有在群组中，或需要使用 text_mention。"
                )
                return None

    return None


def parse_duration(text: str) -> int | None:
    """解析时长（支持格式：30m, 2h, 1d）

    Returns:
        时长（分钟），None 表示永久
    """
    if not text or text.lower() in ["forever", "永久", "0"]:
        return None

    # 匹配格式：数字+单位
    match = re.match(r"(\d+)([mhd])", text.lower())
    if not match:
        return None

    value, unit = match.groups()
    value = int(value)

    # ✅ M6: 限制时长上限，防止极大值注入
    # 最大禁言时间：366 天（Telegram API 限制约为 366 天）
    MAX_DAYS = 366

    if unit == "m":  # 分钟
        minutes = value
    elif unit == "h":  # 小时
        minutes = value * 60
    elif unit == "d":  # 天
        minutes = value * 24 * 60
    else:
        return None

    # 检查是否超过上限
    if minutes > MAX_DAYS * 24 * 60:
        # 超过上限，返回最大值
        return MAX_DAYS * 24 * 60

    return minutes


def parse_spam_args(text: str) -> tuple[bool, str | None]:
    """解析 /spam 命令参数

    支持格式:
        /spam           -> (False, None)
        /spam 原因       -> (False, "原因")
        /spam -d        -> (True, None)
        /spam -d 原因    -> (True, "原因")

    Args:
        text: 完整的命令文本

    Returns:
        (delete_all: bool, reason: str | None)
        - delete_all: 是否删除用户的所有消息
        - reason: 用户提供的原因文本，无原因时为 None（展示层用默认 label）
    """
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return False, None

    args = parts[1].strip()

    # 检查 -d 参数（支持 "-d" 和 "-d原因" 格式）
    if args.startswith("-d"):
        remaining = args[2:].strip()
        return True, remaining or None

    return False, args


def parse_moderation_args(
    text: str, is_reply: bool, default_reason: str | None = None
) -> tuple[bool, str | None]:
    """解析群管理命令参数（kick/mute/ban 通用）

    支持格式（回复消息模式）:
        /command           -> (False, None)
        /command 原因       -> (False, "原因")
        /command -d        -> (True, None)
        /command -d 原因    -> (True, "原因")

    支持格式（命令参数模式）:
        /command <用户ID>           -> (False, None)
        /command <用户ID> 原因       -> (False, "原因")
        /command <用户ID> -d        -> (True, None)
        /command <用户ID> -d 原因    -> (True, "原因")

    Args:
        text: 完整的命令文本
        is_reply: 是否为回复消息模式
        default_reason: 默认原因（可选）

    Returns:
        (delete_all: bool, reason: str | None)
        - delete_all: 是否删除用户的所有消息
        - reason: 原因文本
    """
    parts = text.split(maxsplit=2 if is_reply else 3)

    # 回复消息模式: /command [参数]
    if is_reply:
        if len(parts) < 2:
            return False, default_reason

        args = parts[1].strip()
    # 命令参数模式: /command <用户ID> [参数]
    else:
        if len(parts) < 3:
            return False, default_reason

        args = parts[2].strip()

    # 检查 -d 参数
    if args.startswith("-d"):
        remaining = args[2:].strip()
        return True, remaining if remaining else default_reason

    return False, args if args else default_reason


def parse_mute_args(text: str, is_reply: bool) -> tuple[int | None, str | None]:
    """解析 /mute 命令参数（时长、原因）

    支持格式（回复消息模式）:
        /mute                    -> (None, None)
        /mute 30m                -> (30, None)
        /mute 30m 原因            -> (30, "原因")

    支持格式（命令参数模式）:
        /mute <用户ID>                -> (None, None)
        /mute <用户ID> 30m            -> (30, None)
        /mute <用户ID> 30m 原因        -> (30, "原因")

    Args:
        text: 完整的命令文本
        is_reply: 是否为回复消息模式

    Returns:
        (duration: int | None, reason: str | None)
        - duration: 禁言时长（分钟），None 表示永久
        - reason: 原因文本
    """
    parts = text.split(maxsplit=4)

    # 回复消息模式: /mute [参数...]
    if is_reply:
        if len(parts) < 2:
            return None, None

        # 解析时长
        duration = parse_duration(parts[1])
        reason = None
        if len(parts) > 2:
            reason = " ".join(parts[2:])

        return duration, reason

    # 命令参数模式: /mute <用户ID> [参数...]
    else:
        if len(parts) < 3:
            return None, None

        # 解析时长
        duration = parse_duration(parts[2])
        reason = None
        if len(parts) > 3:
            reason = " ".join(parts[3:])

        return duration, reason


@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """踢出用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        await message.answer(localizer.t("moderation.kick.usage.message"))
        return

    # 解析参数：-d 标志和原因
    if not message.text:
        return

    delete_all, reason = parse_moderation_args(
        text=message.text, is_reply=message.reply_to_message is not None
    )

    # 执行踢出
    result = await ModerationService.kick_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
        revoke_messages=delete_all,
    )

    if result.success:
        # 如果是回复消息模式且未使用 -d 参数，删除被回复的消息
        # （使用 -d 时所有消息已通过 revoke_messages 删除）
        if message.reply_to_message and not delete_all:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        reason_line = (
            localizer.t("moderation.common.reason.line", reason=escape_html(reason))
            if reason
            else ""
        )
        deleted_all = localizer.t("moderation.common.deleted_all.suffix") if delete_all else ""
        reply = await message.answer(
            localizer.t(
                "moderation.kick.success.message",
                target_user_id=target_user_id,
                reason_line=reason_line,
                deleted_all=deleted_all,
            )
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 按 code 渲染本地化错误消息
        assert result.code is not None
        reply = await message.answer(f"❌ {_render_moderation_error(localizer, result.code)}")
        await auto_delete_message(reply)


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """禁言用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        await message.answer(localizer.t("moderation.mute.usage.message"))
        return

    # 解析参数：时长和原因
    if not message.text:
        return

    duration, reason = parse_mute_args(
        text=message.text, is_reply=message.reply_to_message is not None
    )

    # 执行禁言
    result = await ModerationService.mute_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        duration=duration,
        reason=reason,
    )

    if result.success:
        # 如果是回复消息模式，删除被回复的消息
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        duration_text = (
            localizer.t("moderation.duration.permanent.label")
            if duration is None
            else localizer.t("moderation.duration.minutes.label", minutes=duration)
        )
        reason_line = (
            localizer.t("moderation.common.reason.line", reason=escape_html(reason))
            if reason
            else ""
        )
        reply = await message.answer(
            localizer.t(
                "moderation.mute.success.message",
                target_user_id=target_user_id,
                duration=duration_text,
                reason_line=reason_line,
            )
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 按 code 渲染本地化错误消息
        assert result.code is not None
        reply = await message.answer(f"❌ {_render_moderation_error(localizer, result.code)}")
        await auto_delete_message(reply)


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """解除禁言/封禁（与 /unban 等价）

    统一解除用户的所有限制，无论是禁言还是封禁
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        await message.answer(localizer.t("moderation.unmute.usage.message"))
        return

    # 执行解除禁言/封禁
    success = await ModerationService.unmute_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        reply = await message.answer(
            localizer.t("moderation.unmute.success.message", target_user_id=target_user_id)
        )
        await auto_delete_message(reply)
    else:
        reply = await message.answer(localizer.t("moderation.unmute.error.failed.message"))
        await auto_delete_message(reply)


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """封禁用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        await message.answer(localizer.t("moderation.ban.usage.message"))
        return

    # 获取原因和删除标志
    if not message.text:
        return

    delete_all, reason = parse_moderation_args(
        text=message.text, is_reply=message.reply_to_message is not None
    )

    # 执行封禁
    result = await ModerationService.ban_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
        revoke_messages=delete_all,
    )

    if result.success:
        # 如果是回复消息模式且未使用 -d 参数，删除被回复的消息
        # （使用 -d 时所有消息已通过 revoke_messages 删除）
        if message.reply_to_message and not delete_all:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        reason_line = (
            localizer.t("moderation.common.reason.line", reason=escape_html(reason))
            if reason
            else ""
        )
        deleted_all = localizer.t("moderation.common.deleted_all.suffix") if delete_all else ""
        reply = await message.answer(
            localizer.t(
                "moderation.ban.success.message",
                target_user_id=target_user_id,
                reason_line=reason_line,
                deleted_all=deleted_all,
            )
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 按 code 渲染本地化错误消息
        assert result.code is not None
        reply = await message.answer(f"❌ {_render_moderation_error(localizer, result.code)}")
        await auto_delete_message(reply)


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """解除封禁/禁言（与 /unmute 等价）

    统一解除用户的所有限制，无论是禁言还是封禁
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        await message.answer(localizer.t("moderation.unban.usage.message"))
        return

    # 执行解除封禁/禁言
    success = await ModerationService.unban_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        reply = await message.answer(
            localizer.t("moderation.unban.success.message", target_user_id=target_user_id)
        )
        await auto_delete_message(reply)
    else:
        reply = await message.answer(localizer.t("moderation.unmute.error.failed.message"))
        await auto_delete_message(reply)


@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """警告用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        await message.answer(localizer.t("moderation.warn.usage.message"))
        return

    # 获取原因
    if not message.text:
        return

    parts = message.text.split(maxsplit=2)
    # 判断是回复消息还是命令参数模式
    if message.reply_to_message:
        # 回复消息模式: /warn [原因]
        reason = parts[1] if len(parts) > 1 else None
    else:
        # 命令参数模式: /warn <用户ID> [原因]
        reason = parts[2] if len(parts) > 2 else None

    # 检查目标用户是否是管理员
    try:
        target_member = await bot.get_chat_member(message.chat.id, target_user_id)
        if target_member.status in ["creator", "administrator"]:
            reply = await message.answer(
                f"❌ {_render_moderation_error(localizer, ModerationErrorCode.target_is_admin)}"
            )
            await auto_delete_message(reply)
            return
    except Exception as e:
        logger.debug(f"检查目标用户管理员身份失败: {e}")

    # 执行警告
    success, warning_count, auto_punished = await ModerationService.warn_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
    )

    if success:
        reason_line = (
            localizer.t("moderation.common.reason.line", reason=escape_html(reason))
            if reason
            else ""
        )
        punishment_line = ""
        if auto_punished:
            if warning_count >= settings.warning_ban_threshold:
                punishment_line = localizer.t(
                    "moderation.warn.punishment.ban.line",
                    threshold=settings.warning_ban_threshold,
                )
            elif warning_count >= settings.warning_kick_threshold:
                punishment_line = localizer.t(
                    "moderation.warn.punishment.kick.line",
                    threshold=settings.warning_kick_threshold,
                )
            elif warning_count >= settings.max_warnings:
                punishment_line = localizer.t(
                    "moderation.warn.punishment.mute.line",
                    threshold=settings.max_warnings,
                    hours=settings.warning_mute_duration_hours,
                )
        else:
            if warning_count == settings.max_warnings - 1:
                punishment_line = localizer.t(
                    "moderation.warn.next.mute.line",
                    hours=settings.warning_mute_duration_hours,
                )
            elif warning_count == settings.warning_kick_threshold - 1:
                punishment_line = localizer.t("moderation.warn.next.kick.line")
            elif warning_count == settings.warning_ban_threshold - 1:
                punishment_line = localizer.t("moderation.warn.next.ban.line")
        response = (
            localizer.t(
                "moderation.warn.success.message",
                target_user_id=target_user_id,
                warning_count=warning_count,
                expiration_days=settings.warning_expiration_days,
                reason_line=reason_line,
            )
            + punishment_line
        )

        reply = await message.answer(response)
        await auto_delete_message(reply)
    else:
        reply = await message.answer(localizer.t("moderation.warn.error.failed.message"))
        await auto_delete_message(reply)


@router.message(Command("warnings"))
async def cmd_warnings(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """查看用户警告记录"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        # 如果没有指定用户，查看自己的警告
        target_user_id = message.from_user.id

    # ✅ 权限检查：只有管理员可以查看其他用户的警告
    if target_user_id != message.from_user.id:
        # 查看他人警告需要管理员权限
        if not await check_admin_permission_strict_message(message, bot):
            reply = await message.answer(localizer.t("moderation.warnings.admin_required.message"))
            await auto_delete_message(reply)
            return

    # 获取警告列表
    warnings = await UserRepository.get_warnings(message.chat.id, target_user_id)

    if not warnings:
        reply = await message.answer(
            localizer.t("moderation.warnings.empty.message", target_user_id=target_user_id)
        )
        await auto_delete_message(reply)
        return

    # 统计有效期内的警告次数
    recent_count = await UserRepository.count_recent_warnings(
        message.chat.id, target_user_id, days=settings.warning_expiration_days
    )

    # 格式化警告列表
    response = localizer.t(
        "moderation.warnings.summary.message",
        target_user_id=target_user_id,
        recent_count=recent_count,
        expiration_days=settings.warning_expiration_days,
        total=len(warnings),
        max_warnings=settings.max_warnings,
        mute_hours=settings.warning_mute_duration_hours,
        kick_threshold=settings.warning_kick_threshold,
        ban_threshold=settings.warning_ban_threshold,
    )

    for idx, warning in enumerate(warnings[:10], 1):  # 只显示最近10条
        date = warning.created_at.strftime("%Y-%m-%d %H:%M")
        reason = _render_warning_reason(localizer, warning.reason)

        # 判断警告是否在有效期内
        days_ago = (datetime.utcnow() - warning.created_at).days
        if days_ago < settings.warning_expiration_days:
            # 有效警告标记为 ✅
            response += localizer.t(
                "moderation.warnings.row_active.line", idx=idx, date=date, reason=reason
            )
        else:
            # 过期警告标记为 ⏱️
            response += localizer.t(
                "moderation.warnings.row_expired.line", idx=idx, date=date, reason=reason
            )

    if len(warnings) > 10:
        response += localizer.t("moderation.warnings.more.line", count=len(warnings) - 10)

    reply = await message.answer(response)
    await auto_delete_message(reply)


@router.message(Command("clearwarnings"))
async def cmd_clear_warnings(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """清除用户警告"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析目标用户
    target_user_id = await parse_user_from_message(message, bot)
    if target_user_id is None:
        reply = await message.answer(localizer.t("moderation.clearwarnings.usage.message"))
        await auto_delete_message(reply)
        return

    # 清除警告
    success, count = await ModerationService.clear_warnings(
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        reply = await message.answer(
            localizer.t(
                "moderation.clearwarnings.success.message",
                target_user_id=target_user_id,
                count=count,
            )
        )
        await auto_delete_message(reply)
    else:
        reply = await message.answer(localizer.t("moderation.clearwarnings.error.failed.message"))
        await auto_delete_message(reply)


@router.message(Command("delbefore"))
async def cmd_delete_before(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """删除往前（更早）的消息

    用法：回复某条消息，然后使用 /delbefore <N> 删除包含该消息在内的共N条消息
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(localizer.t("moderation.delbefore.usage.message"))
        await auto_delete_message(reply)
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer(localizer.t("moderation.delete.common.count_required.message"))
        await auto_delete_message(reply)
        return

    try:
        count = int(parts[1])
        if count <= 0 or count > 1000:
            reply = await message.answer(
                localizer.t("moderation.delete.common.count_range.message")
            )
            await auto_delete_message(reply)
            return
    except ValueError:
        reply = await message.answer(localizer.t("moderation.delete.common.count_invalid.message"))
        await auto_delete_message(reply)
        return

    # 执行删除
    start_message_id = message.reply_to_message.message_id
    success_count, fail_count = await ModerationService.delete_messages_before(
        bot=bot,
        chat_id=message.chat.id,
        start_message_id=start_message_id,
        count=count,
        operator_id=message.from_user.id,
    )

    reply = await message.answer(
        localizer.t(
            "moderation.delete.common.result.message",
            success_count=success_count,
            fail_count=fail_count,
        )
    )
    await auto_delete_message(reply)


@router.message(Command("delafter"))
async def cmd_delete_after(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """删除往后（更晚）的消息

    用法：回复某条消息，然后使用 /delafter <N> 删除包含该消息在内的共N条消息
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(localizer.t("moderation.delafter.usage.message"))
        await auto_delete_message(reply)
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer(localizer.t("moderation.delete.common.count_required.message"))
        await auto_delete_message(reply)
        return

    try:
        count = int(parts[1])
        if count <= 0 or count > 1000:
            reply = await message.answer(
                localizer.t("moderation.delete.common.count_range.message")
            )
            await auto_delete_message(reply)
            return
    except ValueError:
        reply = await message.answer(localizer.t("moderation.delete.common.count_invalid.message"))
        await auto_delete_message(reply)
        return

    # 执行删除
    start_message_id = message.reply_to_message.message_id
    success_count, fail_count = await ModerationService.delete_messages_after(
        bot=bot,
        chat_id=message.chat.id,
        start_message_id=start_message_id,
        count=count,
        operator_id=message.from_user.id,
    )

    reply = await message.answer(
        localizer.t(
            "moderation.delete.common.result.message",
            success_count=success_count,
            fail_count=fail_count,
        )
    )
    await auto_delete_message(reply)


@router.message(Command("delrange"))
async def cmd_delete_range(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """删除消息范围

    用法：回复起始消息，然后使用 /delrange <结束消息ID或链接> 删除两条消息之间的所有消息
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(localizer.t("moderation.delrange.usage.message"))
        await auto_delete_message(reply)
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        reply = await message.answer(localizer.t("moderation.delrange.error.end_required.message"))
        await auto_delete_message(reply)
        return

    # 解析消息链接，获取群组ID、消息ID和用户名
    link_chat_id, end_message_id, link_username = parse_message_link_with_chat(parts[1])

    if end_message_id is None:
        reply = await message.answer(localizer.t("moderation.delrange.error.invalid_link.message"))
        await auto_delete_message(reply)
        return

    # ✅ 验证链接是否属于当前群组
    # 情况1：私有群组链接 - 通过 chat_id 验证
    if link_chat_id is not None:
        if link_chat_id != message.chat.id:
            reply = await message.answer(
                localizer.t(
                    "moderation.delrange.error.wrong_chat_id.message",
                    link_chat_id=link_chat_id,
                    current_chat_id=message.chat.id,
                )
            )
            await auto_delete_message(reply)
            return
        logger.debug(f"验证通过：私有群组链接属于当前群组 {message.chat.id}")

    # 情况2：公开群组链接 - 通过 username 验证
    elif link_username is not None:
        try:
            # 获取当前群组信息
            current_chat = await bot.get_chat(message.chat.id)
            current_username = current_chat.username

            if current_username is None:
                reply = await message.answer(
                    localizer.t("moderation.delrange.error.no_public_username.message")
                )
                await auto_delete_message(reply)
                return

            # 验证 username 是否匹配（不区分大小写）
            if link_username.lower() != current_username.lower():
                reply = await message.answer(
                    localizer.t(
                        "moderation.delrange.error.wrong_chat_username.message",
                        link_username=escape_html(link_username),
                        current_username=escape_html(current_username),
                    )
                )
                await auto_delete_message(reply)
                return

            logger.debug(
                f"验证通过：公开群组链接属于当前群组 @{current_username} (匹配 @{link_username})"
            )

        except Exception as e:
            logger.error(f"获取群组信息失败: {e}")
            reply = await message.answer(
                localizer.t("moderation.delrange.error.lookup_failed.message")
            )
            await auto_delete_message(reply)
            return

    # 情况3：纯数字 - 表示当前群组内的消息ID，无需验证群组归属
    else:
        logger.debug("使用纯数字消息ID（当前群组内），无需验证群组归属")

    # 执行删除
    start_message_id = message.reply_to_message.message_id

    # 限制删除范围，防止意外删除过多消息
    message_range = abs(end_message_id - start_message_id) + 1
    if message_range > 1000:
        reply = await message.answer(
            localizer.t(
                "moderation.delrange.error.range_too_large.message",
                count=message_range,
            )
        )
        await auto_delete_message(reply)
        return

    success_count, fail_count = await ModerationService.delete_messages_range(
        bot=bot,
        chat_id=message.chat.id,
        start_message_id=start_message_id,
        end_message_id=end_message_id,
        operator_id=message.from_user.id,
    )

    reply = await message.answer(
        localizer.t(
            "moderation.delrange.result.message",
            start=min(start_message_id, end_message_id),
            end=max(start_message_id, end_message_id),
            success_count=success_count,
            fail_count=fail_count,
        )
    )
    await auto_delete_message(reply)


@router.message(Command("spam", "report"))
async def cmd_spam(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """标记垃圾消息

    - 普通用户：创建举报记录，通知管理员
    - 管理员：直接封禁用户并添加到训练库
    - 别名：/report
    """
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(localizer.t("moderation.spam.usage.message"))
        await auto_delete_message(reply)
        return

    # ✅ 修复：检查是否为用户消息（排除频道消息）
    if not message.reply_to_message.from_user:
        reply = await message.answer(localizer.t("moderation.spam.channel_message.message"))
        await auto_delete_message(reply)
        return

    # 获取目标用户ID
    assert message.from_user  # 类型缩小
    target_user_id = message.reply_to_message.from_user.id

    # 解析参数：检测 -d 参数和原因
    delete_all, reason = parse_spam_args(message.text or "")
    spam_reason_label = localizer.t("moderation.spam.reason.default.label")
    reason_display = reason or spam_reason_label

    # 获取消息文本内容
    spam_text = ""
    if message.reply_to_message.text:
        spam_text = message.reply_to_message.text
    elif message.reply_to_message.caption:
        spam_text = message.reply_to_message.caption

    # 如果没有文本内容，记录消息类型
    if not spam_text:
        content_type = message.reply_to_message.content_type
        spam_text = localizer.t(
            "moderation.report.content_type.fallback",
            content_type=escape_html(content_type),
        )

    # 检查是否是管理员
    is_admin = await check_admin_permission_strict_message(message, bot)

    if is_admin:
        # 检查目标用户是否是管理员
        try:
            target_member = await bot.get_chat_member(message.chat.id, target_user_id)
            if target_member.status in ["creator", "administrator"]:
                reply = await message.answer(
                    f"❌ {_render_moderation_error(localizer, ModerationErrorCode.target_is_admin)}"
                )
                await auto_delete_message(reply)
                return
        except Exception as e:
            logger.debug(f"检查目标用户管理员身份失败: {e}")

        # 管理员模式：直接封禁+删除+训练库
        result = await ModerationService.ban_user(
            bot=bot,
            chat_id=message.chat.id,
            user_id=target_user_id,
            operator_id=message.from_user.id,
            reason=f"{spam_reason_label}: {reason}" if reason else spam_reason_label,
            revoke_messages=delete_all,
        )

        if result.success:
            # 删除消息
            # 如果使用 -d，API 已自动删除所有消息
            # 如果不使用 -d，只删除被回复的消息
            if not delete_all:
                try:
                    await message.reply_to_message.delete()
                    logger.debug(f"已删除垃圾消息 [消息ID:{message.reply_to_message.message_id}]")
                except Exception as e:
                    logger.debug(f"删除垃圾消息失败: {e}")

            # 添加到反垃圾训练库
            try:
                await SpamRepository.add_sample(
                    text=spam_text,
                    is_spam=True,
                    confidence=1.0,  # 管理员标注，置信度为1.0
                    labeled_by=message.from_user.id,
                )
                logger.info(
                    f"垃圾样本已添加到训练库 [标注者:{message.from_user.id}] "
                    f"[文本长度:{len(spam_text)}]"
                )
            except Exception as e:
                logger.error(f"添加垃圾样本失败: {e}")

            # 检查是否需要自动训练
            try:
                from src.core.config import settings
                from src.services.spam_detector import get_detector

                detector = get_detector()
                train_result = await detector.check_and_auto_train(admin_ids=settings.admin_ids)
                if train_result is not None:
                    logger.info(f"样本添加后触发自动训练 [结果:{train_result.code.value}]")
            except Exception as e:
                logger.error(f"检查自动训练失败: {e}")

            # 发送响应消息
            reason_line = localizer.t(
                "moderation.common.reason.line",
                reason=escape_html(reason_display),
            )
            deleted_all = localizer.t("moderation.common.deleted_all.suffix") if delete_all else ""
            reply = await message.answer(
                localizer.t(
                    "moderation.spam.processed.message",
                    target_user_id=target_user_id,
                    reason_line=reason_line,
                    deleted_all=deleted_all,
                )
            )
            await auto_delete_message(reply)
        else:
            assert result.code is not None
            reply = await message.answer(f"❌ {_render_moderation_error(localizer, result.code)}")
            await auto_delete_message(reply)
    else:
        # 普通用户模式：创建举报记录
        try:
            # 检查举报频率限制（防止滥用）
            recent_reports = await ReportRepository.count_user_reports(
                group_id=message.chat.id,
                reporter_id=message.from_user.id,
                days=1,
            )

            if recent_reports >= 10:
                reply = await message.answer(localizer.t("moderation.spam.report_limit.message"))
                await auto_delete_message(reply)
                return

            # 创建举报记录
            report = await ReportRepository.create_report(
                group_id=message.chat.id,
                reporter_id=message.from_user.id,
                reported_user_id=target_user_id,
                message_id=message.reply_to_message.message_id,
                message_text=spam_text,
                reason=reason_display,
            )

            # 统计待处理举报数量
            pending_count = await ReportRepository.count_pending_reports(message.chat.id)

            logger.info(
                f"新举报记录 [ID:{report.id}] [举报者:{message.from_user.id}] "
                f"[被举报:{target_user_id}] [原因:{reason}]"
            )

            # 获取管理员 mention
            admin_mentions = await get_chat_administrators_mention(
                bot=bot,
                chat_id=message.chat.id,
            )

            # 创建管理员操作按钮
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=localizer.t("moderation.spam.button.approve.label"),
                            callback_data=f"report_approve:{report.id}",
                        ),
                        InlineKeyboardButton(
                            text=localizer.t("moderation.spam.button.reject.label"),
                            callback_data=f"report_reject:{report.id}",
                        ),
                    ]
                ]
            )

            # 构建消息 header（包含管理员 mention）
            report_header = f"🔔 {admin_mentions}\n\n" if admin_mentions else ""

            reply = await message.answer(
                report_header
                + localizer.t(
                    "moderation.spam.report.submitted.message",
                    report_id=report.id,
                    reason=escape_html(reason_display),
                    pending_count=pending_count,
                ),
                reply_markup=keyboard,
            )
            await auto_delete_message(reply)

        except Exception as e:
            logger.error(f"创建举报记录失败: {e}")
            reply = await message.answer(localizer.t("moderation.spam.submit_failed.message"))
            await auto_delete_message(reply)


@router.message(Command("notspam", "nospam", "unspam"))
async def cmd_notspam(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """标记为非垃圾消息（误报修正 + 预防性训练）

    支持的命令：/notspam, /nospam, /unspam

    支持两种使用方式：
    1. 回复消息：/notspam [备注] - 预防性训练，将正常消息标记为负样本
    2. 指定消息ID：/notspam <message_id> [备注] - 误报修正，从 Redis 获取已删除消息

    仅管理员可用
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查是否是管理员
    if not await check_admin_permission_strict_message(message, bot):
        reply = await message.answer(localizer.t("common.error.permission_denied"))
        await auto_delete_message(reply)
        return

    # 解析命令参数
    if not message.text:
        return

    args = message.text.split(maxsplit=2)

    # 场景判断：回复消息 vs 指定 message_id
    if message.reply_to_message:
        # ==================== 场景A：预防性训练 ====================
        # ✅ 检查是否为用户消息（排除频道消息）
        if not message.reply_to_message.from_user:
            reply = await message.answer(localizer.t("moderation.notspam.channel_message.message"))
            await auto_delete_message(reply)
            return

        # 解析备注（args[1] 是备注）
        note = args[1] if len(args) > 1 else ""

        # 获取消息文本内容
        message_text = ""
        if message.reply_to_message.text:
            message_text = message.reply_to_message.text
        elif message.reply_to_message.caption:
            message_text = message.reply_to_message.caption

        # 如果没有文本内容，记录消息类型
        if not message_text:
            content_type = message.reply_to_message.content_type
            message_text = localizer.t(
                "moderation.report.content_type.fallback",
                content_type=escape_html(content_type),
            )

        usage_type = localizer.t("moderation.notspam.mode.preventive.label")

    else:
        # ==================== 场景B：误报修正（通过 message_id） ====================
        if len(args) < 2:
            reply = await message.answer(localizer.t("moderation.notspam.usage.message"))
            await auto_delete_message(reply)
            return

        # 解析 message_id（支持纯数字和消息链接）
        target_message_id = parse_message_link(args[1])

        if target_message_id is None:
            reply = await message.answer(
                localizer.t(
                    "moderation.notspam.invalid_message_id.message",
                    arg=escape_html(args[1]),
                )
            )
            await auto_delete_message(reply)
            return

        # 解析备注（args[2] 是备注）
        note = args[2] if len(args) > 2 else ""

        # 从 Redis 获取缓存的消息文本
        redis = get_redis()
        text_cache_key = RedisKeys.spam_message_text(message.chat.id, target_message_id)
        cached_text = await redis.get(text_cache_key)

        if not cached_text:
            reply = await message.answer(localizer.t("moderation.notspam.cache_missing.message"))
            await auto_delete_message(reply)
            return

        message_text = cached_text
        usage_type = localizer.t("moderation.notspam.mode.false_positive.label")

    # ==================== 通用处理：添加到训练库 ====================
    try:
        # ✅ 修复 bug：先查找并删除之前的正样本记录（如果存在）
        # 当消息被自动检测为垃圾时，已经调用 add_feedback(is_spam=True) 标记为正样本
        # 现在管理员标记为非垃圾，需要删除之前的正样本，避免数据冲突
        existing_sample = await SpamRepository.find_sample_by_text(message_text, is_spam=True)

        if existing_sample:
            # 删除之前的正样本记录
            deleted = await SpamRepository.delete_sample(existing_sample.id)
            if deleted:
                logger.info(
                    f"notspam 命令：已删除之前的正样本记录 [样本ID:{existing_sample.id}] "
                    f"[文本长度:{len(message_text)}] [{usage_type}]"
                )

        # 添加负样本
        await SpamRepository.add_sample(
            text=message_text,
            is_spam=False,  # 标记为非垃圾
            confidence=1.0,  # 管理员标注，置信度为1.0
            labeled_by=message.from_user.id,
        )
        logger.info(
            f"非垃圾样本已添加到训练库 [{usage_type}] "
            f"[标注者:{message.from_user.id}] "
            f"[文本长度:{len(message_text)}] [备注:{note}]"
        )

        # 检查是否需要自动训练
        try:
            from src.services.spam_detector import get_detector

            detector = get_detector()
            train_result = await detector.check_and_auto_train(admin_ids=settings.admin_ids)
            if train_result is not None:
                logger.info(f"样本添加后触发自动训练 [结果:{train_result.code.value}]")
        except Exception as e:
            logger.error(f"检查自动训练失败: {e}")

        note_line = (
            localizer.t("moderation.notspam.note.line", note=escape_html(note)) if note else ""
        )
        reply = await message.answer(
            localizer.t(
                "moderation.notspam.success.message",
                usage_type=usage_type,
                note_line=note_line,
            )
        )
        await auto_delete_message(reply)

    except Exception as e:
        logger.error(f"添加非垃圾样本失败: {e}")
        reply = await message.answer(localizer.t("moderation.notspam.failed.message"))
        await auto_delete_message(reply)


# ========== 举报处理辅助函数 ==========


def _report_status_label(localizer: BoundLocalizer, status: str | None) -> str:
    """把持久化的举报状态映射为受控的本地化标签。"""
    status_key = status if status in {"pending", "approved", "rejected"} else "unknown"
    return localizer.t(f"moderation.report.status.{status_key}.label")


async def _process_report_approval(
    bot: Bot,
    report_id: int,
    chat_id: int,
    operator_id: int,
    localizer: BoundLocalizer,
) -> tuple[bool, str]:
    """处理举报接受的核心逻辑（供命令和回调共用）。

    返回成功状态及完整的本地化错误文案；成功时错误文案为空。
    """
    try:
        # 获取举报记录
        report = await ReportRepository.get_report_by_id(report_id)

        if not report:
            return False, localizer.t(
                "moderation.report.process.not_found.message",
                report_id=report_id,
            )

        # 检查是否属于当前群组
        if report.group_id != chat_id:
            return False, localizer.t("moderation.report.process.wrong_group.message")

        # 检查状态
        if report.status != "pending":
            return False, localizer.t(
                "moderation.report.process.already_processed.message",
                status=_report_status_label(localizer, report.status),
            )

        # 执行封禁
        result = await ModerationService.ban_user(
            bot=bot,
            chat_id=chat_id,
            user_id=report.reported_user_id,
            operator_id=operator_id,
            reason=f"举报#{report_id}: {report.reason}",
        )

        if not result.success:
            assert result.code is not None
            return False, localizer.t(
                "moderation.report.approval.ban_failed.message",
                error=escape_html(_render_moderation_error(localizer, result.code)),
            )

        # 删除被举报的消息
        try:
            await bot.delete_message(chat_id=chat_id, message_id=report.message_id)
            logger.debug(f"已删除被举报的消息 [消息ID:{report.message_id}]")
        except Exception as e:
            logger.debug(f"删除被举报的消息失败: {e}")

        # 添加到反垃圾训练库
        if report.message_text:
            try:
                await SpamRepository.add_sample(
                    text=report.message_text,
                    is_spam=True,
                    confidence=1.0,
                    labeled_by=operator_id,
                )
                logger.info(
                    f"举报#{report_id}的内容已添加到训练库 [文本长度:{len(report.message_text)}]"
                )
            except Exception as e:
                logger.error(f"添加训练样本失败: {e}")

        # 更新举报状态
        await ReportRepository.update_report_status(
            report_id=report_id,
            status="approved",
            handled_by=operator_id,
        )

        return True, ""

    except Exception as e:
        logger.error(f"处理举报接受失败: {e}")
        return False, localizer.t("moderation.report.process.operation_failed.message")


async def _process_report_rejection(
    report_id: int,
    chat_id: int,
    operator_id: int,
    localizer: BoundLocalizer,
) -> tuple[bool, str]:
    """处理举报拒绝的核心逻辑（供命令和回调共用）。

    返回成功状态及完整的本地化错误文案；成功时错误文案为空。
    """
    try:
        # 获取举报记录
        report = await ReportRepository.get_report_by_id(report_id)

        if not report:
            return False, localizer.t(
                "moderation.report.process.not_found.message",
                report_id=report_id,
            )

        # 检查是否属于当前群组
        if report.group_id != chat_id:
            return False, localizer.t("moderation.report.process.wrong_group.message")

        # 检查状态
        if report.status != "pending":
            return False, localizer.t(
                "moderation.report.process.already_processed.message",
                status=_report_status_label(localizer, report.status),
            )

        # 更新举报状态
        await ReportRepository.update_report_status(
            report_id=report_id,
            status="rejected",
            handled_by=operator_id,
        )

        return True, ""

    except Exception as e:
        logger.error(f"处理举报拒绝失败: {e}")
        return False, localizer.t("moderation.report.process.operation_failed.message")


# ========== 举报查询命令 ==========


@router.message(Command("reports"))
async def cmd_reports(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """查看待处理的举报列表（仅管理员）"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    try:
        # 获取待处理举报
        reports = await ReportRepository.get_pending_reports(message.chat.id, limit=10)

        if not reports:
            reply = await message.answer(localizer.t("moderation.report.empty.message"))
            await auto_delete_message(reply)
            return

        # 构建举报列表
        response = localizer.t("moderation.report.list.header.message", count=len(reports))

        for _idx, report in enumerate(reports, 1):
            # 格式化时间
            time_str = report.created_at.strftime("%m-%d %H:%M")

            # 截断消息文本；无文本用 no_content 占位（保留"无内容"语义）
            if report.message_text:
                text_preview = report.message_text[:50]
                if len(report.message_text) > 50:
                    text_preview += "..."
                content_preview = escape_html(text_preview)
            else:
                content_preview = localizer.t("moderation.report.list.no_content.label")

            reason_display = (
                escape_html(report.reason)
                if report.reason
                else localizer.t("moderation.report.list.no_reason.label")
            )

            response += localizer.t(
                "moderation.report.list.item.message",
                id=report.id,
                time=time_str,
                reporter_id=report.reporter_id,
                reported_user_id=report.reported_user_id,
                reason=reason_display,
                content_preview=content_preview,
                action=f"/approve {report.id}",
            )

        response += localizer.t("moderation.report.list.footer.message")

        reply = await message.answer(response)
        await auto_delete_message(reply, delay=60)  # 60秒后删除

    except Exception as e:
        logger.error(f"获取举报列表失败: {e}")
        reply = await message.answer(localizer.t("moderation.report.fetch_failed.message"))
        await auto_delete_message(reply)


@router.message(Command("approve"))
async def cmd_approve(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """接受举报并执行封禁（仅管理员）

    用法：/approve <report_id>
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer(localizer.t("moderation.approve.usage.message"))
        await auto_delete_message(reply)
        return

    try:
        report_id = int(parts[1])
    except ValueError:
        reply = await message.answer(localizer.t("moderation.approve.invalid_id.message"))
        await auto_delete_message(reply)
        return

    # 调用辅助函数处理
    success, error_msg = await _process_report_approval(
        bot=bot,
        report_id=report_id,
        chat_id=message.chat.id,
        operator_id=message.from_user.id,
        localizer=localizer,
    )

    if success:
        # 获取举报信息用于显示
        report = await ReportRepository.get_report_by_id(report_id)
        reason_display = (
            escape_html(report.reason)
            if report and report.reason
            else localizer.t("moderation.report.list.no_reason.label")
        )
        reply = await message.answer(
            localizer.t(
                "moderation.approve.processed.message",
                report_id=report_id,
                reported_user_id=report.reported_user_id if report else 0,
                reporter_id=report.reporter_id if report else 0,
                reason=reason_display,
            )
        )
        logger.info(f"管理员 {message.from_user.id} 通过命令接受了举报 #{report_id}")
    else:
        reply = await message.answer(error_msg)

    await auto_delete_message(reply)


@router.message(Command("reject"))
async def cmd_reject(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """拒绝举报（仅管理员）

    用法：/reject <report_id>
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("common.error.group_only"))
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer(localizer.t("common.error.permission_denied"))
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer(localizer.t("moderation.reject.usage.message"))
        await auto_delete_message(reply)
        return

    try:
        report_id = int(parts[1])
    except ValueError:
        reply = await message.answer(localizer.t("moderation.reject.invalid_id.message"))
        await auto_delete_message(reply)
        return

    # 调用辅助函数处理
    success, error_msg = await _process_report_rejection(
        report_id=report_id,
        chat_id=message.chat.id,
        operator_id=message.from_user.id,
        localizer=localizer,
    )

    if success:
        # 获取举报详情用于显示
        report = await ReportRepository.get_report_by_id(report_id)
        reason_display = (
            escape_html(report.reason)
            if report and report.reason
            else localizer.t("moderation.report.list.no_reason.label")
        )
        reply = await message.answer(
            localizer.t(
                "moderation.reject.processed.message",
                report_id=report_id,
                reported_user_id=report.reported_user_id if report else 0,
                reporter_id=report.reporter_id if report else 0,
                reason=reason_display,
            )
        )
        await auto_delete_message(reply)
    else:
        reply = await message.answer(error_msg)
        await auto_delete_message(reply)


@router.callback_query(F.data.startswith("report_approve:"))
async def on_report_approve(callback: CallbackQuery, bot: Bot, localizer: BoundLocalizer) -> None:
    """处理举报接受回调（通过按钮）"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                localizer.t("moderation.report.callback.invalid_data.toast"),
                show_alert=True,
            )
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer(
                localizer.t("moderation.report.callback.inaccessible.toast"),
                show_alert=True,
            )
            return

        message: Message = callback.message

        # 解析举报ID
        _, report_id_str = callback.data.split(":")
        report_id = int(report_id_str)

        # 权限验证：只有管理员可以接受
        # ⚠️ 校验实际点击者 callback.from_user；callback.message 由 Bot 发送，
        # 其 from_user 是 Bot 自身，不可作为权限依据（否则任意成员可绕过）
        if not await check_admin_permission_strict(bot, message.chat.id, callback.from_user.id):
            await callback.answer(
                localizer.t("moderation.report.callback.admin_only.approve.toast"),
                show_alert=True,
            )
            return

        # 调用辅助函数处理
        success, error_msg = await _process_report_approval(
            bot=bot,
            report_id=report_id,
            chat_id=message.chat.id,
            operator_id=callback.from_user.id,
            localizer=localizer,
        )

        if success:
            # 获取举报详情用于显示
            report = await ReportRepository.get_report_by_id(report_id)
            reason_display = (
                escape_html(report.reason)
                if report and report.reason
                else localizer.t("moderation.report.list.no_reason.label")
            )

            # 更新消息（移除按钮）
            await message.edit_text(
                localizer.t(
                    "moderation.report.callback.approved.message",
                    report_id=report_id,
                    reason=reason_display,
                    reported_user_id=report.reported_user_id if report else 0,
                    operator=escape_html(callback.from_user.full_name),
                )
            )
            await callback.answer(
                localizer.t("moderation.report.callback.approved.toast"),
                show_alert=True,
            )
        else:
            await callback.answer(error_msg, show_alert=True)

    except Exception as e:
        logger.error(f"处理举报接受回调失败: {e}")
        await callback.answer(
            localizer.t("moderation.report.callback.failed.toast"),
            show_alert=True,
        )


@router.callback_query(F.data.startswith("report_reject:"))
async def on_report_reject(
    callback: CallbackQuery,
    bot: Bot,
    localizer: BoundLocalizer,
) -> None:
    """处理举报拒绝回调（通过按钮）"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                localizer.t("moderation.report.callback.invalid_data.toast"),
                show_alert=True,
            )
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer(
                localizer.t("moderation.report.callback.inaccessible.toast"),
                show_alert=True,
            )
            return

        message: Message = callback.message

        # 解析举报ID
        _, report_id_str = callback.data.split(":")
        report_id = int(report_id_str)

        # 权限验证：只有管理员可以拒绝
        # ⚠️ 校验实际点击者 callback.from_user；callback.message 由 Bot 发送，
        # 其 from_user 是 Bot 自身，不可作为权限依据（否则任意成员可绕过）
        if not await check_admin_permission_strict(bot, message.chat.id, callback.from_user.id):
            await callback.answer(
                localizer.t("moderation.report.callback.admin_only.reject.toast"),
                show_alert=True,
            )
            return

        # 调用辅助函数处理
        success, error_msg = await _process_report_rejection(
            report_id=report_id,
            chat_id=message.chat.id,
            operator_id=callback.from_user.id,
            localizer=localizer,
        )

        if success:
            # 获取举报详情用于显示
            report = await ReportRepository.get_report_by_id(report_id)
            reason_display = (
                escape_html(report.reason)
                if report and report.reason
                else localizer.t("moderation.report.list.no_reason.label")
            )

            # 更新消息（移除按钮）
            await message.edit_text(
                localizer.t(
                    "moderation.report.callback.rejected.message",
                    report_id=report_id,
                    reason=reason_display,
                    reported_user_id=report.reported_user_id if report else 0,
                    operator=escape_html(callback.from_user.full_name),
                )
            )
            await callback.answer(
                localizer.t("moderation.report.callback.rejected.toast"),
                show_alert=True,
            )
        else:
            await callback.answer(error_msg, show_alert=True)

    except Exception as e:
        logger.error(f"处理举报拒绝回调失败: {e}")
        await callback.answer(
            localizer.t("moderation.report.callback.failed.toast"),
            show_alert=True,
        )
