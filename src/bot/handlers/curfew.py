"""宵禁模式命令处理器"""

import re

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.core.utils import auto_delete_message, check_admin_permission
from src.repositories.group_repo import GroupRepository
from src.services.curfew import CurfewService

router = Router(name="curfew")


@router.message(Command("curfew"))
async def cmd_curfew(message: Message, bot: Bot) -> None:
    """宵禁模式配置命令

    用法:
    - /curfew 23:00 7:00 - 启用宵禁 (23:00-07:00)，时区默认 +8
    - /curfew 23:00 7:00 +9 - 启用宵禁，指定时区 +9
    - /curfew 23 7 - 启用宵禁（分钟可选）
    - /curfew 23 7 +8 - 启用宵禁，指定时区
    - /curfew off - 禁用宵禁
    - /curfew - 查看当前状态
    """
    assert message.from_user
    assert message.chat

    # 检查是否在群组中
    if message.chat.type == "private":
        reply = await message.answer("❌ 此命令只能在群组中使用")
        await auto_delete_message(reply)
        return

    # 检查管理员权限
    if not await check_admin_permission(message, bot):
        reply = await message.answer("❌ 只有管理员可以使用此命令")
        await auto_delete_message(reply)
        return

    # 解析命令
    text = message.text or ""
    parts = text.split()[1:]  # 移除 /curfew

    group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

    # 显示状态
    if not parts:
        if not group.curfew_enabled or group.curfew_start_hour is None:
            reply = await message.answer(
                "🌙 <b>宵禁模式状态</b>\n\n"
                "当前状态: ❌ 未启用\n\n"
                "💡 <b>使用方法:</b>\n"
                "• /curfew 23:00 7:00 - 启用宵禁（时区默认 +8）\n"
                "• /curfew 23:00 7:00 +9 - 启用宵禁（指定时区）\n"
                "• /curfew 23 7 - 启用宵禁（分钟可选）\n"
                "• /curfew off - 禁用宵禁",
                parse_mode="HTML",
            )
        else:
            is_in_curfew = CurfewService.is_in_curfew(group)
            status_emoji = "🌙" if is_in_curfew else "☀️"
            status_text = "宵禁中" if is_in_curfew else "非宵禁时段"

            reply = await message.answer(
                f"🌙 <b>宵禁模式状态</b>\n\n"
                f"当前状态: ✅ 已启用\n"
                f"宵禁时间: {group.curfew_start_hour:02d}:{group.curfew_start_minute:02d} - "
                f"{group.curfew_end_hour:02d}:{group.curfew_end_minute:02d}\n"
                f"时区: UTC{group.curfew_timezone_offset:+d}\n"
                f"当前: {status_emoji} {status_text}\n\n"
                f"📋 <b>限制规则:</b>\n"
                f"• 活跃度 = 0: 无法发送任何消息\n"
                f"• 活跃度 &lt; 10: 无法发送非文本消息\n"
                f"• 活跃度 &gt;= 10: 可正常发送消息",
                parse_mode="HTML",
            )
        await auto_delete_message(reply)
        return

    # 禁用宵禁
    if parts[0].lower() == "off":
        await GroupRepository.update_curfew_settings(message.chat.id, enabled=False)
        reply = await message.answer("✅ 宵禁模式已禁用")
        await auto_delete_message(reply)
        logger.info(f"群组 {message.chat.id} 已禁用宵禁模式")
        return

    # 启用宵禁
    if len(parts) < 2:
        reply = await message.answer(
            "❌ 参数错误\n\n"
            "用法: /curfew &lt;开始时间&gt; &lt;结束时间&gt; [时区]\n"
            "示例: /curfew 23:00 7:00 或 /curfew 23 7 +8"
        )
        await auto_delete_message(reply)
        return

    # 解析时间
    try:
        # 解析开始时间
        start_match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", parts[0])
        if not start_match:
            raise ValueError("开始时间格式错误")
        start_hour = int(start_match.group(1))
        start_minute = int(start_match.group(2)) if start_match.group(2) else 0

        # 解析结束时间
        end_match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", parts[1])
        if not end_match:
            raise ValueError("结束时间格式错误")
        end_hour = int(end_match.group(1))
        end_minute = int(end_match.group(2)) if end_match.group(2) else 0

        # 解析时区（可选）
        timezone_offset = 8  # 默认 +8
        if len(parts) >= 3:
            tz_match = re.match(r"^([+-]?\d{1,2})$", parts[2])
            if not tz_match:
                raise ValueError("时区格式错误")
            timezone_offset = int(tz_match.group(1))

        # 验证范围
        if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59):
            raise ValueError("开始时间超出范围")
        if not (0 <= end_hour <= 23 and 0 <= end_minute <= 59):
            raise ValueError("结束时间超出范围")
        if not (-12 <= timezone_offset <= 14):
            raise ValueError("时区偏移超出范围（-12 到 +14）")

    except ValueError as e:
        reply = await message.answer(
            f"❌ {e}\n\n时间格式: HH:MM 或 HH (0-23小时, 0-59分钟)\n时区格式: +8 或 -5"
        )
        await auto_delete_message(reply)
        return

    # 更新设置
    await GroupRepository.update_curfew_settings(
        message.chat.id,
        enabled=True,
        start_hour=start_hour,
        start_minute=start_minute,
        end_hour=end_hour,
        end_minute=end_minute,
        timezone_offset=timezone_offset,
    )

    # 检查当前是否在宵禁期
    current_group = await GroupRepository.get(message.chat.id)
    is_in_curfew = CurfewService.is_in_curfew(current_group) if current_group else False
    status_text = "（当前正处于宵禁时段）" if is_in_curfew else "（当前不在宵禁时段）"

    reply = await message.answer(
        f"✅ 宵禁模式已启用\n\n"
        f"宵禁时间: {start_hour:02d}:{start_minute:02d} - {end_hour:02d}:{end_minute:02d}\n"
        f"时区: UTC{timezone_offset:+d}\n"
        f"{status_text}\n\n"
        f"📋 <b>限制规则:</b>\n"
        f"• 活跃度 = 0: 无法发送任何消息\n"
        f"• 活跃度 &lt; 10: 无法发送非文本消息\n"
        f"• 活跃度 &gt;= 10: 可正常发送消息",
        parse_mode="HTML",
    )
    await auto_delete_message(reply)
    logger.info(
        f"群组 {message.chat.id} 已启用宵禁模式: "
        f"{start_hour:02d}:{start_minute:02d} - {end_hour:02d}:{end_minute:02d} "
        f"(UTC{timezone_offset:+d})"
    )
