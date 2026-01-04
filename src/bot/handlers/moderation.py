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
from src.core.utils import escape_html, auto_delete_message, check_admin_permission, parse_message_link
from src.core.cache import PermissionCache  # ✅ P1-10: 导入权限缓存

router = Router(name="moderation")


def parse_user_from_message(message: Message) -> Optional[int]:
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
        return message.reply_to_message.from_user.id

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
                    f"检测到 @username 格式但无法解析用户ID。"
                    f"可能原因：用户没有在群组中，或需要使用 text_mention。"
                )
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
            "❌ 请指定要踢出的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /kick <用户ID>\n"
            "方式3: /kick @用户（在输入框中 @ 并从列表中选择）"
        )
        return

    # 获取原因
    parts = message.text.split(maxsplit=2)
    # 判断是回复消息还是命令参数模式
    if message.reply_to_message:
        # 回复消息模式: /kick [原因]
        reason = parts[1] if len(parts) > 1 else None
    else:
        # 命令参数模式: /kick <用户ID> [原因]
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
        # 如果是回复消息模式，删除被回复的消息
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        reply = await message.answer(
            f"✅ 已踢出用户 {target_user_id}" + (f"\n原因: {escape_html(reason)}" if reason else "")
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 显示详细的错误消息
        reply = await message.answer(f"❌ {error_msg}")
        await auto_delete_message(reply)


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
            "❌ 请指定要禁言的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /mute <用户ID> [时长] [原因]\n"
            "方式3: /mute @用户 [时长] [原因]\n\n"
            "时长格式: 30m (30分钟), 2h (2小时), 1d (1天), 不填为永久"
        )
        return

    # 解析时长和原因
    parts = message.text.split(maxsplit=3)
    duration = None
    reason = None

    # 判断是回复消息还是命令参数模式
    if message.reply_to_message:
        # 回复消息模式: /mute [时长] [原因]
        # parts[0] = "/mute", parts[1] = 时长, parts[2] = 原因
        if len(parts) > 1:
            duration = parse_duration(parts[1])
        if len(parts) > 2:
            reason = parts[2]
    else:
        # 命令参数模式: /mute <用户ID> [时长] [原因]
        # parts[0] = "/mute", parts[1] = 用户ID, parts[2] = 时长, parts[3] = 原因
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
            "❌ 请指定要解除禁言的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /unmute <用户ID>\n"
            "方式3: /unmute @用户"
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
        reply = await message.answer(f"✅ 已解除用户 {target_user_id} 的禁言")
        await auto_delete_message(reply)
    else:
        reply = await message.answer("❌ 操作失败，请检查Bot权限")
        await auto_delete_message(reply)


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
            "❌ 请指定要封禁的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /ban <用户ID> [原因]\n"
            "方式3: /ban @用户 [原因]"
        )
        return

    # 获取原因
    parts = message.text.split(maxsplit=2)
    # 判断是回复消息还是命令参数模式
    if message.reply_to_message:
        # 回复消息模式: /ban [原因]
        reason = parts[1] if len(parts) > 1 else None
    else:
        # 命令参数模式: /ban <用户ID> [原因]
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
        # 如果是回复消息模式，删除被回复的消息
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
                logger.debug(f"已删除被回复的消息 [消息ID:{message.reply_to_message.message_id}]")
            except Exception as e:
                logger.debug(f"删除被回复的消息失败: {e}")

        reply = await message.answer(
            f"✅ 已封禁用户 {target_user_id}" + (f"\n原因: {escape_html(reason)}" if reason else "")
        )
        await auto_delete_message(reply)
    else:
        # ✅ M7: 显示详细的错误消息
        reply = await message.answer(f"❌ {error_msg}")
        await auto_delete_message(reply)


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
            "❌ 请指定要解除封禁的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /unban <用户ID>\n"
            "方式3: /unban @用户"
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
        reply = await message.answer(f"✅ 已解除用户 {target_user_id} 的封禁")
        await auto_delete_message(reply)
    else:
        reply = await message.answer("❌ 操作失败，请检查Bot权限或用户未被封禁")
        await auto_delete_message(reply)


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
            "❌ 请指定要警告的用户：\n\n"
            "方式1: 回复用户的消息\n"
            "方式2: /warn <用户ID> [原因]\n"
            "方式3: /warn @用户 [原因]"
        )
        return

    # 获取原因
    parts = message.text.split(maxsplit=2)
    # 判断是回复消息还是命令参数模式
    if message.reply_to_message:
        # 回复消息模式: /warn [原因]
        reason = parts[1] if len(parts) > 1 else None
    else:
        # 命令参数模式: /warn <用户ID> [原因]
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

        reply = await message.answer(response)
        await auto_delete_message(reply)
    else:
        reply = await message.answer("❌ 操作失败")
        await auto_delete_message(reply)


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
            reply = await message.answer("❌ 只有管理员可以查看其他用户的警告记录")
            await auto_delete_message(reply)
            return

    # 获取警告列表
    warnings = await UserRepository.get_warnings(message.chat.id, target_user_id)

    if not warnings:
        reply = await message.answer(f"✅ 用户 {target_user_id} 没有警告记录")
        await auto_delete_message(reply)
        return

    # 格式化警告列表
    response = f"⚠️ 用户 {target_user_id} 的警告记录（共 {len(warnings)} 次）:\n\n"

    for idx, warning in enumerate(warnings[:10], 1):  # 只显示最近10条
        date = warning.created_at.strftime("%Y-%m-%d %H:%M")
        reason = escape_html(warning.reason) if warning.reason else "无原因"
        response += f"{idx}. [{date}] {reason}\n"

    if len(warnings) > 10:
        response += f"\n... 还有 {len(warnings) - 10} 条历史记录"

    reply = await message.answer(response)
    await auto_delete_message(reply)


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
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(
            "❌ 请回复要删除的消息\\n\\n"
            "<b>用法</b>: 回复某条消息，然后使用 /delbefore &lt;数量&gt;\\n"
            "<b>示例</b>: /delbefore 10  (删除包含该消息在内往前共10条消息)"
        )
        await auto_delete_message(reply)
        return

    # 解析参数
    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer("❌ 请指定要删除的消息数量")
        await auto_delete_message(reply)
        return

    try:
        count = int(parts[1])
        if count <= 0 or count > 100:
            reply = await message.answer("❌ 删除数量必须在 1-100 之间")
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
        f"✅ 删除完成\\n"
        f"成功: {success_count} 条\\n"
        f"失败: {fail_count} 条"
    )
    await auto_delete_message(reply)


@router.message(Command("delafter"))
async def cmd_delete_after(message: Message, bot: Bot) -> None:
    """删除往后（更晚）的消息

    用法：回复某条消息，然后使用 /delafter <N> 删除包含该消息在内的共N条消息
    """
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(
            "❌ 请回复要删除的消息\\n\\n"
            "<b>用法</b>: 回复某条消息，然后使用 /delafter &lt;数量&gt;\\n"
            "<b>示例</b>: /delafter 10  (删除包含该消息在内往后共10条消息)"
        )
        await auto_delete_message(reply)
        return

    # 解析参数
    parts = message.text.split()
    if len(parts) < 2:
        reply = await message.answer("❌ 请指定要删除的消息数量")
        await auto_delete_message(reply)
        return

    try:
        count = int(parts[1])
        if count <= 0 or count > 100:
            reply = await message.answer("❌ 删除数量必须在 1-100 之间")
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
        f"✅ 删除完成\\n"
        f"成功: {success_count} 条\\n"
        f"失败: {fail_count} 条"
    )
    await auto_delete_message(reply)


@router.message(Command("delrange"))
async def cmd_delete_range(message: Message, bot: Bot) -> None:
    """删除消息范围

    用法：回复起始消息，然后使用 /delrange <结束消息ID或链接> 删除两条消息之间的所有消息
    """
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 必须回复某条消息
    if not message.reply_to_message:
        reply = await message.answer(
            "❌ 请回复起始消息\\n\\n"
            "<b>用法</b>: 回复起始消息，然后使用 /delrange &lt;结束消息ID或链接&gt;\\n\\n"
            "<b>示例1</b>: /delrange 12345\\n"
            "<b>示例2</b>: /delrange https://t.me/c/1234567890/12345\\n\\n"
            "💡 <b>提示</b>: 在电脑端右键点击消息选择\"复制消息链接\"，然后直接粘贴即可"
        )
        await auto_delete_message(reply)
        return

    # 解析参数
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        reply = await message.answer("❌ 请指定结束消息ID或消息链接")
        await auto_delete_message(reply)
        return

    # 使用 parse_message_link 解析消息ID或链接
    end_message_id = parse_message_link(parts[1])

    if end_message_id is None:
        reply = await message.answer(
            "❌ 无法解析消息ID\\n\\n"
            "支持的格式：\\n"
            "1. 纯数字：12345\\n"
            "2. 消息链接：https://t.me/c/1234567890/12345"
        )
        await auto_delete_message(reply)
        return

    # 执行删除
    start_message_id = message.reply_to_message.message_id

    # 限制删除范围，防止意外删除过多消息
    message_range = abs(end_message_id - start_message_id) + 1
    if message_range > 1000:
        reply = await message.answer(
            f"❌ 删除范围过大（{message_range} 条消息）\\n"
            "为了安全，单次最多删除 1000 条消息"
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
        f"✅ 删除完成\\n"
        f"消息范围: {min(start_message_id, end_message_id)} - {max(start_message_id, end_message_id)}\\n"
        f"成功: {success_count} 条\\n"
        f"失败: {fail_count} 条"
    )
    await auto_delete_message(reply)

