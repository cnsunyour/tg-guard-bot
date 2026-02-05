"""群管理命令处理器"""

import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis
from src.core.utils import (
    auto_delete_message,
    check_admin_permission_strict_message,
    escape_html,
    parse_message_link,
    parse_message_link_with_chat,
)
from src.repositories.report_repo import ReportRepository
from src.repositories.spam_repo import SpamRepository
from src.repositories.user_repo import UserRepository
from src.services.moderation import ModerationService

router = Router(name="moderation")


def parse_user_from_message(message: Message) -> int | None:
    """从消息中解析用户ID

    支持的格式：
    1. 回复消息
    2. 用户ID：/command 123456
    3. @提及用户（text_mention）：/command @user

    Returns:
        用户ID，如果无法解析则返回 None
    """
    # 1. 检查是否回复了某条消息
    if message.reply_to_message:
        # ✅ 修复：检查是否为用户消息（排除频道消息）
        if message.reply_to_message.from_user:
            return message.reply_to_message.from_user.id
        # 如果是频道消息，返回 None
        return None

    # 2. 检查 entities 中是否有 text_mention（用户被 @ 提及）
    if message.entities:
        for entity in message.entities:
            # text_mention: 用户没有用户名，通过客户端点击选择用户时的提及
            # 包含完整的 User 对象
            if entity.type == "text_mention" and entity.user:
                logger.debug(f"通过 text_mention 解析到用户: {entity.user.id}")
                return entity.user.id

    # 3. 检查命令参数中是否有用户ID
    if message.text:
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


def parse_spam_args(text: str) -> tuple[bool, str]:
    """解析 /spam 命令参数

    支持格式:
        /spam           -> (False, "垃圾消息")
        /spam 原因       -> (False, "原因")
        /spam -d        -> (True, "垃圾消息")
        /spam -d 原因    -> (True, "原因")

    Args:
        text: 完整的命令文本

    Returns:
        (delete_all: bool, reason: str)
        - delete_all: 是否删除用户的所有消息
        - reason: 原因文本
    """
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return False, "垃圾消息"

    args = parts[1].strip()

    # 检查 -d 参数（支持 "-d" 和 "-d原因" 格式）
    if args.startswith("-d"):
        remaining = args[2:].strip()
        return True, remaining if remaining else "垃圾消息"

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
async def cmd_kick(message: Message, bot: Bot) -> None:
    """踢出用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请指定要踢出的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /kick <用户ID> [原因]\n"
            "方式3: /kick @用户 [原因]\n\n"
            "<b>新功能</b>:\n"
            "• /kick -d [原因] - 踢出用户，<b>删除该用户的所有消息</b>\n"
            "• /kick <用户ID> -d [原因] - 同上"
        )
        return

    # 解析参数：-d 标志和原因
    if not message.text:
        return

    delete_all, reason = parse_moderation_args(
        text=message.text, is_reply=message.reply_to_message is not None
    )

    # 执行踢出
    success, error_msg = await ModerationService.kick_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
        revoke_messages=delete_all,
    )

    if success:
        # 如果是回复消息模式且未使用 -d 参数，删除被回复的消息
        # （使用 -d 时所有消息已通过 revoke_messages 删除）
        if message.reply_to_message and not delete_all:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        reply = await message.answer(
            f"✅ 已踢出用户 {target_user_id}"
            + (f"\n原因: {escape_html(reason)}" if reason else "")
            + (" (已删除所有消息)" if delete_all else "")
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 显示详细的错误消息
        reply = await message.answer(f"❌ {error_msg}")
        await auto_delete_message(reply)


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot) -> None:
    """禁言用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请指定要禁言的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /mute <用户ID> [时长] [原因]\n"
            "方式3: /mute @用户 [时长] [原因]\n\n"
            "时长格式: 30m (30分钟), 2h (2小时), 1d (1天), 不填为永久"
        )
        return

    # 解析参数：时长和原因
    if not message.text:
        return

    duration, reason = parse_mute_args(
        text=message.text, is_reply=message.reply_to_message is not None
    )

    # 执行禁言
    success, error_msg = await ModerationService.mute_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        duration=duration,
        reason=reason,
    )

    if success:
        # 如果是回复消息模式，删除被回复的消息
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        duration_text = "永久" if duration is None else f"{duration}分钟"
        reply = await message.answer(
            f"✅ 已禁言用户 {target_user_id}，时长: {duration_text}"
            + (f"\n原因: {escape_html(reason)}" if reason else "")
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 显示详细的错误消息
        reply = await message.answer(f"❌ {error_msg}")
        await auto_delete_message(reply)


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot) -> None:
    """解除禁言/封禁（与 /unban 等价）

    统一解除用户的所有限制，无论是禁言还是封禁
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请指定要解除限制的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /unmute <用户ID>\n"
            "方式3: /unmute @用户\n\n"
            "💡 提示：/unmute 和 /unban 功能完全相同"
        )
        return

    # 执行解除禁言/封禁
    success = await ModerationService.unmute_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        reply = await message.answer(f"✅ 已解除用户 {target_user_id} 的所有限制（禁言/封禁）")
        await auto_delete_message(reply)
    else:
        reply = await message.answer("❌ 操作失败，请检查Bot权限")
        await auto_delete_message(reply)


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot) -> None:
    """封禁用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请指定要封禁的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /ban <用户ID> [原因]\n"
            "方式3: /ban @用户 [原因]\n\n"
            "<b>新功能</b>:\n"
            "• /ban -d [原因] - 封禁用户，<b>删除该用户的所有消息</b>\n"
            "• /ban <用户ID> -d [原因] - 同上"
        )
        return

    # 获取原因和删除标志
    if not message.text:
        return

    delete_all, reason = parse_moderation_args(
        text=message.text, is_reply=message.reply_to_message is not None
    )

    # 执行封禁
    success, error_msg = await ModerationService.ban_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
        revoke_messages=delete_all,
    )

    if success:
        # 如果是回复消息模式且未使用 -d 参数，删除被回复的消息
        # （使用 -d 时所有消息已通过 revoke_messages 删除）
        if message.reply_to_message and not delete_all:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        reply = await message.answer(
            f"✅ 已封禁用户 {target_user_id}"
            + (f"\n原因: {escape_html(reason)}" if reason else "")
            + (" (已删除所有消息)" if delete_all else "")
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 显示详细的错误消息
        reply = await message.answer(f"❌ {error_msg}")
        await auto_delete_message(reply)


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot) -> None:
    """解除封禁/禁言（与 /unmute 等价）

    统一解除用户的所有限制，无论是禁言还是封禁
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请指定要解除限制的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /unban <用户ID>\n"
            "方式3: /unban @用户\n\n"
            "💡 提示：/unban 和 /unmute 功能完全相同"
        )
        return

    # 执行解除封禁/禁言
    success = await ModerationService.unban_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        reply = await message.answer(f"✅ 已解除用户 {target_user_id} 的所有限制（禁言/封禁）")
        await auto_delete_message(reply)
    else:
        reply = await message.answer("❌ 操作失败，请检查Bot权限或用户未被封禁")
        await auto_delete_message(reply)


@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot) -> None:
    """警告用户"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请指定要警告的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /warn <用户ID> [原因]\n"
            "方式3: /warn @用户 [原因]"
        )
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
            reply = await message.answer("❌ 无法对群组管理员执行此操作")
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
        response = (
            f"⚠️ 已警告用户 {target_user_id}\n"
            f"有效警告: {warning_count} "
            f"({settings.warning_expiration_days}天内)"
        )
        if reason:
            # ✅ P1-8: 转义用户输入的原因，防止 HTML 注入
            response += f"\n原因: {escape_html(reason)}"

        # 根据警告次数显示处罚提示
        if auto_punished:
            if warning_count >= settings.warning_ban_threshold:
                response += f"\n\n🚫 用户已达到 {settings.warning_ban_threshold} 次警告，已被封禁"
            elif warning_count >= settings.warning_kick_threshold:
                response += f"\n\n👢 用户已达到 {settings.warning_kick_threshold} 次警告，已被踢出"
            elif warning_count >= settings.max_warnings:
                response += (
                    f"\n\n🔇 用户已达到 {settings.max_warnings} 次警告，"
                    f"自动禁言 {settings.warning_mute_duration_hours} 小时"
                )

        # 显示下一阶段处罚提示
        else:
            if warning_count == settings.max_warnings - 1:
                response += (
                    f"\n\n💡 提示：再 1 次警告将自动禁言 "
                    f"{settings.warning_mute_duration_hours} 小时"
                )
            elif warning_count == settings.warning_kick_threshold - 1:
                response += "\n\n💡 提示：再 1 次警告将被踢出群组"
            elif warning_count == settings.warning_ban_threshold - 1:
                response += "\n\n💡 提示：再 1 次警告将被封禁"

        reply = await message.answer(response)
        await auto_delete_message(reply)
    else:
        reply = await message.answer("❌ 操作失败")
        await auto_delete_message(reply)


@router.message(Command("warnings"))
async def cmd_warnings(message: Message, bot: Bot) -> None:
    """查看用户警告记录"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        # 如果没有指定用户，查看自己的警告
        target_user_id = message.from_user.id

    # ✅ 权限检查：只有管理员可以查看其他用户的警告
    if target_user_id != message.from_user.id:
        # 查看他人警告需要管理员权限
        if not await check_admin_permission_strict_message(message, bot):
            reply = await message.answer("❌ 只有管理员可以查看其他用户的警告记录")
            await auto_delete_message(reply)
            return

    # 获取警告列表
    warnings = await UserRepository.get_warnings(message.chat.id, target_user_id)

    if not warnings:
        reply = await message.answer(f"✅ 用户 {target_user_id} 没有警告记录")
        await auto_delete_message(reply)
        return

    # 统计有效期内的警告次数
    recent_count = await UserRepository.count_recent_warnings(
        message.chat.id, target_user_id, days=settings.warning_expiration_days
    )

    # 格式化警告列表
    response = (
        f"⚠️ 用户 {target_user_id} 的警告记录:\n"
        f"有效警告: {recent_count} ({settings.warning_expiration_days}天内)\n"
        f"历史记录: {len(warnings)} 次\n\n"
        f"📋 处罚阶梯:\n"
        f"• {settings.max_warnings} 次 → 禁言 {settings.warning_mute_duration_hours} 小时\n"
        f"• {settings.warning_kick_threshold} 次 → 踢出群组\n"
        f"• {settings.warning_ban_threshold} 次 → 封禁（拉黑）\n\n"
    )

    for idx, warning in enumerate(warnings[:10], 1):  # 只显示最近10条
        date = warning.created_at.strftime("%Y-%m-%d %H:%M")
        reason = escape_html(warning.reason) if warning.reason else "无原因"

        # 判断警告是否在有效期内
        days_ago = (datetime.utcnow() - warning.created_at).days
        if days_ago < settings.warning_expiration_days:
            # 有效警告标记为 ✅
            response += f"{idx}. ✅ [{date}] {reason}\n"
        else:
            # 过期警告标记为 ⏱️
            response += f"{idx}. ⏱️ [{date}] {reason} (已过期)\n"

    if len(warnings) > 10:
        response += f"\n... 还有 {len(warnings) - 10} 条历史记录"

    reply = await message.answer(response)
    await auto_delete_message(reply)


@router.message(Command("clearwarnings"))
async def cmd_clear_warnings(message: Message, bot: Bot) -> None:
    """清除用户警告"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        reply = await message.answer(
            "❌ 请指定要清除警告的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /clearwarnings <用户ID>\n"
            "方式3: /clearwarnings @用户"
        )
        await auto_delete_message(reply)
        return

    # 清除警告
    success, count = await ModerationService.clear_warnings(
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        reply = await message.answer(f"✅ 已清除用户 {target_user_id} 的 {count} 条警告记录")
        await auto_delete_message(reply)
    else:
        reply = await message.answer("❌ 操作失败")
        await auto_delete_message(reply)


@router.message(Command("delbefore"))
async def cmd_delete_before(message: Message, bot: Bot) -> None:
    """删除往前（更早）的消息

    用法：回复某条消息，然后使用 /delbefore <N> 删除包含该消息在内的共N条消息
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(
            "❌ 请回复要删除的消息\n\n"
            "<b>用法</b>: 回复某条消息，然后使用 /delbefore &lt;数量&gt;\n"
            "<b>示例</b>: /delbefore 10  (删除包含该消息在内往前共10条消息)"
        )
        await auto_delete_message(reply)
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer("❌ 请指定要删除的消息数量")
        await auto_delete_message(reply)
        return

    try:
        count = int(parts[1])
        if count <= 0 or count > 1000:
            reply = await message.answer("❌ 删除数量必须在 1-1000 之间")
            await auto_delete_message(reply)
            return
    except ValueError:
        reply = await message.answer("❌ 删除数量必须是数字")
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
        f"✅ 删除完成\n" f"成功: {success_count} 条\n" f"失败: {fail_count} 条"
    )
    await auto_delete_message(reply)


@router.message(Command("delafter"))
async def cmd_delete_after(message: Message, bot: Bot) -> None:
    """删除往后（更晚）的消息

    用法：回复某条消息，然后使用 /delafter <N> 删除包含该消息在内的共N条消息
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(
            "❌ 请回复要删除的消息\n\n"
            "<b>用法</b>: 回复某条消息，然后使用 /delafter &lt;数量&gt;\n"
            "<b>示例</b>: /delafter 10  (删除包含该消息在内往后共10条消息)"
        )
        await auto_delete_message(reply)
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer("❌ 请指定要删除的消息数量")
        await auto_delete_message(reply)
        return

    try:
        count = int(parts[1])
        if count <= 0 or count > 1000:
            reply = await message.answer("❌ 删除数量必须在 1-1000 之间")
            await auto_delete_message(reply)
            return
    except ValueError:
        reply = await message.answer("❌ 删除数量必须是数字")
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
        f"✅ 删除完成\n" f"成功: {success_count} 条\n" f"失败: {fail_count} 条"
    )
    await auto_delete_message(reply)


@router.message(Command("delrange"))
async def cmd_delete_range(message: Message, bot: Bot) -> None:
    """删除消息范围

    用法：回复起始消息，然后使用 /delrange <结束消息ID或链接> 删除两条消息之间的所有消息
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(
            "❌ 请回复起始消息\n\n"
            "<b>用法</b>: 回复起始消息，然后使用 /delrange &lt;结束消息ID或链接&gt;\n\n"
            "<b>示例1</b>: /delrange 12345\n"
            "<b>示例2</b>: /delrange https://t.me/c/1234567890/12345\n\n"
            '💡 <b>提示</b>: 在电脑端右键点击消息选择"复制消息链接"，然后直接粘贴即可'
        )
        await auto_delete_message(reply)
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        reply = await message.answer("❌ 请指定结束消息ID或消息链接")
        await auto_delete_message(reply)
        return

    # 解析消息链接，获取群组ID、消息ID和用户名
    link_chat_id, end_message_id, link_username = parse_message_link_with_chat(parts[1])

    if end_message_id is None:
        reply = await message.answer(
            "❌ 无法解析消息ID\n\n"
            "支持的格式：\n"
            "1. 纯数字：12345\n"
            "2. 私有群组链接：https://t.me/c/1234567890/12345\n"
            "3. 公开群组链接：https://t.me/groupname/12345"
        )
        await auto_delete_message(reply)
        return

    # ✅ 验证链接是否属于当前群组
    # 情况1：私有群组链接 - 通过 chat_id 验证
    if link_chat_id is not None:
        if link_chat_id != message.chat.id:
            reply = await message.answer(
                "❌ 消息链接不属于当前群组\n\n"
                f"链接所属群组ID: {link_chat_id}\n"
                f"当前群组ID: {message.chat.id}\n\n"
                "💡 提示：请确保复制的是本群组的消息链接"
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
                    "❌ 当前群组未设置公开用户名，无法验证公开链接\n\n"
                    "💡 提示：\n"
                    "• 对于私有群组，请使用消息ID或私有链接\n"
                    "• 或在群组设置中添加公开用户名"
                )
                await auto_delete_message(reply)
                return

            # 验证 username 是否匹配（不区分大小写）
            if link_username.lower() != current_username.lower():
                reply = await message.answer(
                    "❌ 消息链接不属于当前群组\n\n"
                    f"链接所属群组: @{link_username}\n"
                    f"当前群组: @{current_username}\n\n"
                    "💡 提示：请确保复制的是本群组的消息链接"
                )
                await auto_delete_message(reply)
                return

            logger.debug(
                f"验证通过：公开群组链接属于当前群组 @{current_username} "
                f"(匹配 @{link_username})"
            )

        except Exception as e:
            logger.error(f"获取群组信息失败: {e}")
            reply = await message.answer("❌ 验证群组信息失败，请稍后重试")
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
            f"❌ 删除范围过大（{message_range} 条消息）\n" "为了安全，单次最多删除 1000 条消息"
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
        f"✅ 删除完成\n"
        f"消息范围: {min(start_message_id, end_message_id)} - {max(start_message_id, end_message_id)}\n"
        f"成功: {success_count} 条\n"
        f"失败: {fail_count} 条"
    )
    await auto_delete_message(reply)


@router.message(Command("spam", "report"))
async def cmd_spam(message: Message, bot: Bot) -> None:
    """标记垃圾消息

    - 普通用户：创建举报记录，通知管理员
    - 管理员：直接封禁用户并添加到训练库
    - 别名：/report
    """
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(
            "❌ 请回复要标记为垃圾的消息\n\n"
            "<b>用法</b>:\n"
            "• /spam [原因] - 封禁用户，删除被回复的消息\n"
            "• /spam -d [原因] - 封禁用户，<b>删除该用户的所有消息</b>\n\n"
            "<b>示例</b>:\n"
            "• /spam 发送广告\n"
            "• /spam -d 大量发送垃圾信息\n\n"
            "💡 <b>说明</b>:\n"
            "• 普通用户：创建举报记录\n"
            "• 管理员：直接封禁并添加到训练库"
        )
        await auto_delete_message(reply)
        return

    # ✅ 修复：检查是否为用户消息（排除频道消息）
    if not message.reply_to_message.from_user:
        reply = await message.answer("❌ 无法举报频道消息，请回复用户消息")
        await auto_delete_message(reply)
        return

    # 获取目标用户ID
    assert message.from_user  # 类型缩小
    target_user_id = message.reply_to_message.from_user.id

    # 解析参数：检测 -d 参数和原因
    delete_all, reason = parse_spam_args(message.text or "")

    # 获取消息文本内容
    spam_text = ""
    if message.reply_to_message.text:
        spam_text = message.reply_to_message.text
    elif message.reply_to_message.caption:
        spam_text = message.reply_to_message.caption

    # 如果没有文本内容，记录消息类型
    if not spam_text:
        content_type = message.reply_to_message.content_type
        spam_text = f"[{content_type}消息]"

    # 检查是否是管理员
    is_admin = await check_admin_permission_strict_message(message, bot)

    if is_admin:
        # 检查目标用户是否是管理员
        try:
            target_member = await bot.get_chat_member(message.chat.id, target_user_id)
            if target_member.status in ["creator", "administrator"]:
                reply = await message.answer("❌ 无法对群组管理员执行此操作")
                await auto_delete_message(reply)
                return
        except Exception as e:
            logger.debug(f"检查目标用户管理员身份失败: {e}")

        # 管理员模式：直接封禁+删除+训练库
        success, error_msg = await ModerationService.ban_user(
            bot=bot,
            chat_id=message.chat.id,
            user_id=target_user_id,
            operator_id=message.from_user.id,
            reason=f"垃圾消息: {reason}",
            revoke_messages=delete_all,
        )

        if success:
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
                triggered, train_message = await detector.check_and_auto_train(
                    admin_ids=settings.admin_ids
                )
                if triggered:
                    logger.info(f"样本添加后触发自动训练: {train_message}")
            except Exception as e:
                logger.error(f"检查自动训练失败: {e}")

            # 发送响应消息
            if delete_all:
                reply = await message.answer(
                    f"✅ 已处理垃圾消息\n"
                    f"• 用户已封禁: {target_user_id}\n"
                    f"• 已删除该用户的所有消息\n"
                    f"• 已添加到训练库\n"
                    f"• 原因: {escape_html(reason)}"
                )
            else:
                reply = await message.answer(
                    f"✅ 已处理垃圾消息\n"
                    f"• 用户已封禁: {target_user_id}\n"
                    f"• 消息已删除\n"
                    f"• 已添加到训练库\n"
                    f"• 原因: {escape_html(reason)}"
                )
            await auto_delete_message(reply)
        else:
            reply = await message.answer(f"❌ {error_msg}")
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
                reply = await message.answer(
                    "❌ 您今天的举报次数已达上限（10次）\n" "如有紧急情况，请联系管理员"
                )
                await auto_delete_message(reply)
                return

            # 创建举报记录
            report = await ReportRepository.create_report(
                group_id=message.chat.id,
                reporter_id=message.from_user.id,
                reported_user_id=target_user_id,
                message_id=message.reply_to_message.message_id,
                message_text=spam_text,
                reason=reason,
            )

            # 统计待处理举报数量
            pending_count = await ReportRepository.count_pending_reports(message.chat.id)

            logger.info(
                f"新举报记录 [ID:{report.id}] [举报者:{message.from_user.id}] "
                f"[被举报:{target_user_id}] [原因:{reason}]"
            )

            # 创建管理员操作按钮
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ 接受", callback_data=f"report_approve:{report.id}"
                        ),
                        InlineKeyboardButton(
                            text="❌ 拒绝", callback_data=f"report_reject:{report.id}"
                        ),
                    ]
                ]
            )

            reply = await message.answer(
                f"✅ 举报已提交\n"
                f"• 举报ID: #{report.id}\n"
                f"• 原因: {escape_html(reason)}\n"
                f"• 待处理举报: {pending_count} 条\n\n"
                f"💡 管理员可点击按钮快速处理",
                reply_markup=keyboard,
            )
            await auto_delete_message(reply)

        except Exception as e:
            logger.error(f"创建举报记录失败: {e}")
            reply = await message.answer("❌ 举报提交失败，请稍后重试")
            await auto_delete_message(reply)


@router.message(Command("notspam", "nospam", "unspam"))
async def cmd_notspam(message: Message, bot: Bot) -> None:
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
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查是否是管理员
    if not await check_admin_permission_strict_message(message, bot):
        reply = await message.answer("❌ 只有管理员可以使用此命令")
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
            reply = await message.answer("❌ 无法标记频道消息，请回复用户消息")
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
            message_text = f"[{content_type}消息]"

        usage_type = "预防性训练"

    else:
        # ==================== 场景B：误报修正（通过 message_id） ====================
        if len(args) < 2:
            reply = await message.answer(
                "❌ 请提供消息ID或回复要标记的消息\n\n"
                "<b>用法</b>:\n"
                "• /notspam [备注] - 回复消息，预防性训练\n"
                "• /notspam &lt;消息ID或链接&gt; [备注] - 标记已删除消息为误报\n\n"
                "<b>支持的格式</b>:\n"
                "• 纯数字：12345\n"
                "• 私有群组链接：https://t.me/c/1234567890/12345\n"
                "• 公开群组链接：https://t.me/channel_name/12345\n\n"
                "<b>示例</b>:\n"
                "• /notspam （回复正常消息）\n"
                "• /notspam 这是正常讨论 （回复正常消息）\n"
                "• /notspam 12345 （标记已删除的消息12345为误报）\n"
                "• /notspam 12345 这是误报 （标记已删除消息并添加备注）\n"
                "• /notspam https://t.me/c/1234567890/12345 （使用消息链接）\n\n"
                "💡 <b>说明</b>:\n"
                "• 仅管理员可用\n"
                "• 预防性训练：增强模型对正常消息的识别\n"
                "• 误报修正：修正被误判的消息（警告消息删除后仍可用）"
            )
            await auto_delete_message(reply)
            return

        # 解析 message_id（支持纯数字和消息链接）
        target_message_id = parse_message_link(args[1])

        if target_message_id is None:
            reply = await message.answer(
                f"❌ 无法解析消息ID: {escape_html(args[1])}\n\n"
                "支持的格式：\n"
                "• 纯数字：/notspam 12345\n"
                "• 私有群组链接：/notspam https://t.me/c/1234567890/12345\n"
                "• 公开群组链接：/notspam https://t.me/channel_name/12345"
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
            reply = await message.answer(
                "❌ 未找到该消息的缓存\n\n"
                "可能原因：\n"
                "• 消息ID不正确\n"
                "• 缓存已过期（1小时后自动删除）\n"
                "• 该消息未被检测为垃圾"
            )
            await auto_delete_message(reply)
            return

        message_text = cached_text
        usage_type = "误报修正"

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
            triggered, train_message = await detector.check_and_auto_train(
                admin_ids=settings.admin_ids, threshold=50
            )
            if triggered:
                logger.info(f"样本添加后触发自动训练: {train_message}")
        except Exception as e:
            logger.error(f"检查自动训练失败: {e}")

        reply = await message.answer(
            f"✅ 已标记为正常消息（{usage_type}）\n"
            "• 已添加到训练库（负样本）\n"
            "• 帮助模型避免类似误判\n"
            + (f"• 备注: {escape_html(note)}\n" if note else "")
            + "\n💡 积累足够样本后会自动触发训练"
        )
        await auto_delete_message(reply)

    except Exception as e:
        logger.error(f"添加非垃圾样本失败: {e}")
        reply = await message.answer("❌ 操作失败，请稍后重试")
        await auto_delete_message(reply)


# ========== 举报处理辅助函数 ==========


async def _process_report_approval(
    bot: Bot,
    report_id: int,
    chat_id: int,
    operator_id: int,
) -> tuple[bool, str]:
    """处理举报接受的核心逻辑（供命令和回调共用）

    Args:
        bot: Bot 实例
        report_id: 举报ID
        chat_id: 群组ID
        operator_id: 操作者ID

    Returns:
        (success: bool, message: str) - 成功状态和消息
    """
    try:
        # 获取举报记录
        report = await ReportRepository.get_report_by_id(report_id)

        if not report:
            return False, f"未找到举报记录 #{report_id}"

        # 检查是否属于当前群组
        if report.group_id != chat_id:
            return False, "此举报不属于当前群组"

        # 检查状态
        if report.status != "pending":
            return False, f"此举报已被处理（状态: {report.status}）"

        # 执行封禁
        success, error_msg = await ModerationService.ban_user(
            bot=bot,
            chat_id=chat_id,
            user_id=report.reported_user_id,
            operator_id=operator_id,
            reason=f"举报#{report_id}: {report.reason}",
        )

        if not success:
            return False, f"封禁失败: {error_msg}"

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
                    f"举报#{report_id}的内容已添加到训练库 "
                    f"[文本长度:{len(report.message_text)}]"
                )
            except Exception as e:
                logger.error(f"添加训练样本失败: {e}")

        # 更新举报状态
        await ReportRepository.update_report_status(
            report_id=report_id,
            status="approved",
            handled_by=operator_id,
        )

        return True, "举报已接受并处理"

    except Exception as e:
        logger.error(f"处理举报接受失败: {e}")
        return False, f"处理失败: {e!s}"


async def _process_report_rejection(
    report_id: int,
    chat_id: int,
    operator_id: int,
) -> tuple[bool, str]:
    """处理举报拒绝的核心逻辑（供命令和回调共用）

    Args:
        report_id: 举报ID
        chat_id: 群组ID
        operator_id: 操作者ID

    Returns:
        (success: bool, message: str) - 成功状态和消息
    """
    try:
        # 获取举报记录
        report = await ReportRepository.get_report_by_id(report_id)

        if not report:
            return False, f"未找到举报记录 #{report_id}"

        # 检查是否属于当前群组
        if report.group_id != chat_id:
            return False, "此举报不属于当前群组"

        # 检查状态
        if report.status != "pending":
            return False, f"此举报已被处理（状态: {report.status}）"

        # 更新举报状态
        await ReportRepository.update_report_status(
            report_id=report_id,
            status="rejected",
            handled_by=operator_id,
        )

        return True, "举报已拒绝"

    except Exception as e:
        logger.error(f"处理举报拒绝失败: {e}")
        return False, f"处理失败: {e!s}"


# ========== 举报查询命令 ==========


@router.message(Command("reports"))
async def cmd_reports(message: Message, bot: Bot) -> None:
    """查看待处理的举报列表（仅管理员）"""
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    try:
        # 获取待处理举报
        reports = await ReportRepository.get_pending_reports(message.chat.id, limit=10)

        if not reports:
            reply = await message.answer("✅ 当前没有待处理的举报")
            await auto_delete_message(reply)
            return

        # 构建举报列表
        response = f"📋 <b>待处理举报</b> (共 {len(reports)} 条)\n\n"

        for _idx, report in enumerate(reports, 1):
            # 格式化时间
            time_str = report.created_at.strftime("%m-%d %H:%M")

            # 截断消息文本
            text_preview = report.message_text[:50] if report.message_text else "[无文本]"
            if len(report.message_text or "") > 50:
                text_preview += "..."

            response += (
                f"<b>#{report.id}</b> [{time_str}]\n"
                f"• 举报者: {report.reporter_id}\n"
                f"• 被举报: {report.reported_user_id}\n"
                f"• 原因: {escape_html(report.reason or '无')}\n"
                f"• 内容: {escape_html(text_preview)}\n"
                f"• 操作: /approve {report.id}\n\n"
            )

        response += "💡 使用 /approve <ID> 处理举报"

        reply = await message.answer(response)
        await auto_delete_message(reply, delay=60)  # 60秒后删除

    except Exception as e:
        logger.error(f"获取举报列表失败: {e}")
        reply = await message.answer("❌ 获取举报列表失败")
        await auto_delete_message(reply)


@router.message(Command("approve"))
async def cmd_approve(message: Message, bot: Bot) -> None:
    """接受举报并执行封禁（仅管理员）

    用法：/approve <report_id>
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer(
            "❌ 请指定举报ID\n\n"
            "<b>用法</b>: /approve &lt;举报ID&gt;\n"
            "<b>示例</b>: /approve 123"
        )
        await auto_delete_message(reply)
        return

    try:
        report_id = int(parts[1])
    except ValueError:
        reply = await message.answer("❌ 举报ID必须是数字")
        await auto_delete_message(reply)
        return

    # 调用辅助函数处理
    success, msg = await _process_report_approval(
        bot=bot,
        report_id=report_id,
        chat_id=message.chat.id,
        operator_id=message.from_user.id,
    )

    if success:
        # 获取举报信息用于显示
        report = await ReportRepository.get_report_by_id(report_id)
        reply = await message.answer(
            f"✅ 举报#{report_id}已处理\n"
            f"• 用户已封禁: {(report.reported_user_id if report else 0)}\n"
            f"• 消息已删除\n"
            f"• 已添加到训练库\n"
            f"• 举报者: {(report.reporter_id if report else 0)}\n"
            f"• 原因: {escape_html((report.reason if report else "未知") or '无')}"
        )
        logger.info(f"管理员 {message.from_user.id} 通过命令接受了举报 #{report_id}")
    else:
        reply = await message.answer(f"❌ {msg}")

    await auto_delete_message(reply)


@router.message(Command("reject"))
async def cmd_reject(message: Message, bot: Bot) -> None:
    """拒绝举报（仅管理员）

    用法：/reject <report_id>
    """
    if not message.from_user:
        return

    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission_strict_message(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析参数
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer(
            "❌ 请指定举报ID\n\n" "<b>用法</b>: /reject &lt;举报ID&gt;\n" "<b>示例</b>: /reject 123"
        )
        await auto_delete_message(reply)
        return

    try:
        report_id = int(parts[1])
    except ValueError:
        reply = await message.answer("❌ 举报ID必须是数字")
        await auto_delete_message(reply)
        return

    # 调用辅助函数处理
    success, msg = await _process_report_rejection(
        report_id=report_id,
        chat_id=message.chat.id,
        operator_id=message.from_user.id,
    )

    if success:
        # 获取举报详情用于显示
        report = await ReportRepository.get_report_by_id(report_id)
        reply = await message.answer(
            f"✅ 举报#{report_id}已拒绝\n"
            f"• 被举报用户: {(report.reported_user_id if report else 0)}\n"
            f"• 举报者: {(report.reporter_id if report else 0)}\n"
            f"• 原因: {escape_html((report.reason if report else "未知") or '无')}\n\n"
            f"💡 此举报已被标记为误报或不需要处理"
        )
        await auto_delete_message(reply)
    else:
        reply = await message.answer(f"❌ {msg}")
        await auto_delete_message(reply)


@router.callback_query(F.data.startswith("report_approve:"))
async def on_report_approve(callback: CallbackQuery, bot: Bot) -> None:
    """处理举报接受回调（通过按钮）"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        # 解析举报ID
        _, report_id_str = callback.data.split(":")
        report_id = int(report_id_str)

        # 权限验证：只有管理员可以接受
        if not await check_admin_permission_strict_message(message, bot):
            await callback.answer("❌ 只有管理员可以接受举报", show_alert=True)
            return

        # 调用辅助函数处理
        success, msg = await _process_report_approval(
            bot=bot,
            report_id=report_id,
            chat_id=message.chat.id,
            operator_id=callback.from_user.id,
        )

        if success:
            # 获取举报详情用于显示
            report = await ReportRepository.get_report_by_id(report_id)

            # 更新消息（移除按钮）
            await message.edit_text(
                f"✅ 举报已接受处理\n"
                f"• 举报ID: #{report_id}\n"
                f"• 原因: {escape_html((report.reason if report else "未知") or '无')}\n"
                f"• 被举报用户: {(report.reported_user_id if report else 0)}\n"
                f"• 处理者: {callback.from_user.full_name}\n\n"
                f"✓ 用户已封禁\n"
                f"✓ 消息已删除\n"
                f"✓ 已添加到训练库"
            )
            await callback.answer("✅ 举报已接受并处理", show_alert=True)
        else:
            await callback.answer(f"❌ {msg}", show_alert=True)

    except Exception as e:
        logger.error(f"处理举报接受回调失败: {e}")
        await callback.answer("❌ 处理失败，请稍后重试", show_alert=True)


@router.callback_query(F.data.startswith("report_reject:"))
async def on_report_reject(callback: CallbackQuery, bot: Bot) -> None:
    """处理举报拒绝回调（通过按钮）"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        # 解析举报ID
        _, report_id_str = callback.data.split(":")
        report_id = int(report_id_str)

        # 权限验证：只有管理员可以拒绝
        if not await check_admin_permission_strict_message(message, bot):
            await callback.answer("❌ 只有管理员可以拒绝举报", show_alert=True)
            return

        # 调用辅助函数处理
        success, msg = await _process_report_rejection(
            report_id=report_id,
            chat_id=message.chat.id,
            operator_id=callback.from_user.id,
        )

        if success:
            # 获取举报详情用于显示
            report = await ReportRepository.get_report_by_id(report_id)

            # 更新消息（移除按钮）
            await message.edit_text(
                f"❌ 举报已拒绝\n"
                f"• 举报ID: #{report_id}\n"
                f"• 原因: {escape_html((report.reason if report else "未知") or '无')}\n"
                f"• 被举报用户: {(report.reported_user_id if report else 0)}\n"
                f"• 处理者: {callback.from_user.full_name}\n\n"
                f"💡 此举报已被标记为误报或不需要处理"
            )
            await callback.answer("✅ 举报已拒绝", show_alert=True)
        else:
            await callback.answer(f"❌ {msg}", show_alert=True)

    except Exception as e:
        logger.error(f"处理举报拒绝回调失败: {e}")
        await callback.answer("❌ 处理失败，请稍后重试", show_alert=True)
