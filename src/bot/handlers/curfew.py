"""宵禁模式命令处理器"""

import re

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.core.i18n import BoundLocalizer
from src.core.utils import auto_delete_message, check_admin_permission
from src.repositories.group_repo import GroupRepository
from src.services.curfew import CurfewService

router = Router(name="curfew")


def _parse_curfew_schedule(
    parts: list[str],
) -> tuple[tuple[int, int, int, int, int] | None, str | None]:
    """解析 /curfew 启用参数，失败返回稳定 validation code。

    成功返回 ``(schedule, None)``，``schedule`` 为
    ``(start_hour, start_minute, end_hour, end_minute, timezone_offset)``；
    失败返回 ``(None, code)``，``code`` 对应 ``curfew.error.{code}.message``，
    由调用方按 locale 渲染（不再把中文塞进异常再展示）。
    """
    if len(parts) < 2:
        return None, "missing_schedule"

    start_match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", parts[0])
    if not start_match:
        return None, "start_format"
    start_hour = int(start_match.group(1))
    start_minute = int(start_match.group(2)) if start_match.group(2) else 0

    end_match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", parts[1])
    if not end_match:
        return None, "end_format"
    end_hour = int(end_match.group(1))
    end_minute = int(end_match.group(2)) if end_match.group(2) else 0

    timezone_offset = 8  # 默认 +8
    if len(parts) >= 3:
        timezone_match = re.match(r"^([+-]?\d{1,2})$", parts[2])
        if not timezone_match:
            return None, "timezone_format"
        timezone_offset = int(timezone_match.group(1))

    if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59):
        return None, "start_range"
    if not (0 <= end_hour <= 23 and 0 <= end_minute <= 59):
        return None, "end_range"
    if not (-12 <= timezone_offset <= 14):
        return None, "timezone_range"

    return (start_hour, start_minute, end_hour, end_minute, timezone_offset), None


def _format_time(hour: int, minute: int) -> str:
    """格式化 HH:MM。catalog 不支持 ``{hour:02d}`` format spec，须先转字符串。"""
    return f"{hour:02d}:{minute:02d}"


def _state_label(localizer: BoundLocalizer, is_in_curfew: bool) -> str:
    """当前宵禁状态的本地化标签（含 emoji）。"""
    state = "active" if is_in_curfew else "inactive"
    return localizer.t(f"curfew.state.{state}.label")


@router.message(Command("curfew"))
async def cmd_curfew(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
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
        reply = await message.answer(localizer.t("common.error.group_only"))
        await auto_delete_message(reply)
        return

    # 检查管理员权限
    if not await check_admin_permission(message, bot):
        reply = await message.answer(localizer.t("common.error.permission_denied"))
        await auto_delete_message(reply)
        return

    # 解析命令
    text = message.text or ""
    parts = text.split()[1:]  # 移除 /curfew

    group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

    # 显示状态
    if not parts:
        if (
            not group.curfew_enabled
            or group.curfew_start_hour is None
            or group.curfew_start_minute is None
            or group.curfew_end_hour is None
            or group.curfew_end_minute is None
        ):
            reply = await message.answer(
                localizer.t("curfew.status.disabled.message"),
                parse_mode="HTML",
            )
        else:
            is_in_curfew = CurfewService.is_in_curfew(group)
            reply = await message.answer(
                localizer.t(
                    "curfew.status.enabled.message",
                    start_time=_format_time(group.curfew_start_hour, group.curfew_start_minute),
                    end_time=_format_time(group.curfew_end_hour, group.curfew_end_minute),
                    timezone=f"{group.curfew_timezone_offset:+d}",
                    state=_state_label(localizer, is_in_curfew),
                    rules=localizer.t("curfew.rules.summary.message"),
                ),
                parse_mode="HTML",
            )
        await auto_delete_message(reply)
        return

    # 禁用宵禁
    if parts[0].lower() == "off":
        await GroupRepository.update_curfew_settings(message.chat.id, enabled=False)
        reply = await message.answer(localizer.t("curfew.command.disabled.message"))
        await auto_delete_message(reply)
        logger.info(f"群组 {message.chat.id} 已禁用宵禁模式")
        return

    # 解析时间（失败返回稳定 code，按 locale 渲染错误文案）
    schedule, validation_code = _parse_curfew_schedule(parts)
    if validation_code is not None:
        reply = await message.answer(
            localizer.t(f"curfew.error.{validation_code}.message"),
            parse_mode="HTML",
        )
        await auto_delete_message(reply)
        return
    assert schedule is not None
    start_hour, start_minute, end_hour, end_minute, timezone_offset = schedule

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
    start_time = _format_time(start_hour, start_minute)
    end_time = _format_time(end_hour, end_minute)
    timezone = f"{timezone_offset:+d}"

    reply = await message.answer(
        localizer.t(
            "curfew.command.enabled.message",
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            state=_state_label(localizer, is_in_curfew),
            rules=localizer.t("curfew.rules.summary.message"),
        ),
        parse_mode="HTML",
    )
    await auto_delete_message(reply)
    logger.info(f"群组 {message.chat.id} 已启用宵禁模式: {start_time} - {end_time} (UTC{timezone})")
