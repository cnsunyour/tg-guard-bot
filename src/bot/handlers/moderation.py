"""群管理命令处理器"""

import re
from typing import Optional

from aiogram import Router, Bot, F
from aiogram.types import Message
from aiogram.filters import Command
from loguru import logger

from src.services.moderation import ModerationService
from src.repositories.user_repo import UserRepository
from src.core.config import settings
from src.core.utils import escape_html
from src.core.cache import PermissionCache  # ✅ P1-10: 导入权限缓存

router = Router(name="moderation")


def parse_user_from_message(message: Message) -> Optional[int]:
    """从消息中解析用户ID"""
    # 检查是否回复了某条消息
    if message.reply_to_message:
        return message.reply_to_message.from_user.id

    # 检查命令参数中是否有用户ID或用户名
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip()

            # 尝试解析 @username
            if arg.startswith("@"):
                # 这里需要通过 Telegram API 查询用户名对应的ID
                # 暂时返回 None，后续可以改进
                return None

            # 尝试解析纯数字ID
            if arg.isdigit():
                user_id = int(arg)
                # ✅ M1: 验证 Telegram 用户 ID 范围（1 到 2^63-1）
                if 1 <= user_id <= 9223372036854775807:
                    return user_id
                else:
                    # 无效的用户 ID
                    return None

    return None


def parse_duration(text: str) -> Optional[int]:
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


async def check_admin_permission(message: Message, bot: Bot) -> bool:
    """检查是否是管理员

    ✅ P1-10: 使用 Redis 缓存减少 API 调用
    """
    # 检查是否在配置的管理员列表中
    if message.from_user.id in settings.admin_ids:
        return True

    # 使用缓存检查是否是群组管理员
    return await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id)


@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot) -> None:
    """踢出用户"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请回复要踢出的用户消息，或使用格式：\n"
            "/kick <用户ID>\n"
            "/kick @username (暂不支持)"
        )
        return

    # 获取原因
    parts = message.text.split(maxsplit=2)
    reason = parts[2] if len(parts) > 2 else None

    # 执行踢出
    success, error_msg = await ModerationService.kick_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
    )

    if success:
        await message.answer(
            f"✅ 已踢出用户 {target_user_id}" + (f"\n原因: {escape_html(reason)}" if reason else "")
        )
    else:
        # ✅ M7: 显示详细的错误消息
        await message.answer(f"❌ {error_msg}")


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot) -> None:
    """禁言用户"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请回复要禁言的用户消息，或使用格式：\n"
            "/mute <用户ID> [时长] [原因]\n"
            "时长格式: 30m (30分钟), 2h (2小时), 1d (1天), 不填为永久"
        )
        return

    # 解析时长和原因
    parts = message.text.split(maxsplit=3)
    duration = None
    reason = None

    if len(parts) > 2:
        duration = parse_duration(parts[2])

    if len(parts) > 3:
        reason = parts[3]

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
        duration_text = "永久" if duration is None else f"{duration}分钟"
        await message.answer(
            f"✅ 已禁言用户 {target_user_id}，时长: {duration_text}"
            + (f"\n原因: {escape_html(reason)}" if reason else "")
        )
    else:
        # ✅ M7: 显示详细的错误消息
        await message.answer(f"❌ {error_msg}")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot) -> None:
    """解除禁言"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请回复要解除禁言的用户消息，或使用格式：\n" "/unmute <用户ID>"
        )
        return

    # 执行解除禁言
    success = await ModerationService.unmute_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        await message.answer(f"✅ 已解除用户 {target_user_id} 的禁言")
    else:
        await message.answer("❌ 操作失败，请检查Bot权限")


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot) -> None:
    """封禁用户"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请回复要封禁的用户消息，或使用格式：\n" "/ban <用户ID> [原因]"
        )
        return

    # 获取原因
    parts = message.text.split(maxsplit=2)
    reason = parts[2] if len(parts) > 2 else None

    # 执行封禁
    success, error_msg = await ModerationService.ban_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
    )

    if success:
        await message.answer(
            f"✅ 已封禁用户 {target_user_id}" + (f"\n原因: {escape_html(reason)}" if reason else "")
        )
    else:
        # ✅ M7: 显示详细的错误消息
        await message.answer(f"❌ {error_msg}")


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot) -> None:
    """解除封禁"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请回复要解除封禁的用户消息，或使用格式：\n" "/unban <用户ID>"
        )
        return

    # 执行解除封禁
    success = await ModerationService.unban_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        await message.answer(f"✅ 已解除用户 {target_user_id} 的封禁")
    else:
        await message.answer("❌ 操作失败，请检查Bot权限或用户未被封禁")


@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot) -> None:
    """警告用户"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请回复要警告的用户消息，或使用格式：\n" "/warn <用户ID> [原因]"
        )
        return

    # 获取原因
    parts = message.text.split(maxsplit=2)
    reason = parts[2] if len(parts) > 2 else None

    # 执行警告
    success, warning_count, auto_muted = await ModerationService.warn_user(
        bot=bot,
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
        reason=reason,
    )

    if success:
        response = (
            f"⚠️ 已警告用户 {target_user_id}\n"
            f"累计警告: {warning_count}/{settings.max_warnings}"
        )
        if reason:
            # ✅ P1-8: 转义用户输入的原因，防止 HTML 注入
            response += f"\n原因: {escape_html(reason)}"

        if auto_muted:
            response += f"\n\n🔇 用户已达到 {settings.max_warnings} 次警告，自动禁言 24 小时"

        await message.answer(response)
    else:
        await message.answer("❌ 操作失败")


@router.message(Command("warnings"))
async def cmd_warnings(message: Message, bot: Bot) -> None:
    """查看用户警告记录"""
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
        if not await check_admin_permission(message, bot):
            await message.answer("❌ 只有管理员可以查看其他用户的警告记录")
            return

    # 获取警告列表
    warnings = await UserRepository.get_warnings(message.chat.id, target_user_id)

    if not warnings:
        await message.answer(f"✅ 用户 {target_user_id} 没有警告记录")
        return

    # 格式化警告列表
    response = f"⚠️ 用户 {target_user_id} 的警告记录（共 {len(warnings)} 次）:\n\n"

    for idx, warning in enumerate(warnings[:10], 1):  # 只显示最近10条
        date = warning.created_at.strftime("%Y-%m-%d %H:%M")
        reason = escape_html(warning.reason) if warning.reason else "无原因"
        response += f"{idx}. [{date}] {reason}\n"

    if len(warnings) > 10:
        response += f"\n... 还有 {len(warnings) - 10} 条历史记录"

    await message.answer(response)


@router.message(Command("clearwarnings"))
async def cmd_clear_warnings(message: Message, bot: Bot) -> None:
    """清除用户警告"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 解析目标用户
    target_user_id = parse_user_from_message(message)
    if target_user_id is None:
        await message.answer(
            "❌ 请回复要清除警告的用户消息，或使用格式：\n" "/clearwarnings <用户ID>"
        )
        return

    # 清除警告
    success, count = await ModerationService.clear_warnings(
        chat_id=message.chat.id,
        user_id=target_user_id,
        operator_id=message.from_user.id,
    )

    if success:
        await message.answer(f"✅ 已清除用户 {target_user_id} 的 {count} 条警告记录")
    else:
        await message.answer("❌ 操作失败")
