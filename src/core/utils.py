"""通用工具函数模块"""

import html
import asyncio
from typing import Optional
from loguru import logger


def escape_html(text: Optional[str]) -> str:
    """转义 HTML 特殊字符，防止 HTML 注入

    Args:
        text: 待转义的文本

    Returns:
        转义后的安全文本
    """
    if not text:
        return ""
    return html.escape(str(text))


def format_user_mention(user) -> str:
    """安全地格式化用户提及，防止 HTML 注入

    Args:
        user: Telegram User 对象

    Returns:
        格式化的安全用户提及字符串
    """
    # 转义用户名
    name = escape_html(user.full_name or user.first_name or "Unknown")

    # 用户名或 ID
    if user.username:
        identifier = f"@{user.username}"
    else:
        identifier = f"ID:{user.id}"

    return f"{name} ({identifier})"


def mask_text(text: Optional[str], show_length: int = 10) -> str:
    """脱敏文本内容，用于日志记录

    Args:
        text: 待脱敏的文本
        show_length: 显示的字符长度

    Returns:
        脱敏后的文本
    """
    if not text:
        return "***"

    text_str = str(text)

    if len(text_str) <= show_length:
        return "***"

    return f"{text_str[:show_length]}...*** (length: {len(text_str)})"


def parse_time_to_seconds(time_str: str) -> Optional[int]:
    """解析时间字符串为秒数

    Args:
        time_str: 时间字符串，支持格式: 30m, 2h, 1d, forever

    Returns:
        秒数，forever 返回 None

    Raises:
        ValueError: 无效的时间格式
    """
    if not time_str or not isinstance(time_str, str):
        raise ValueError("时间字符串不能为空")

    time_str = time_str.strip().lower()

    # 永久封禁
    if time_str in ("forever", "permanent", "永久"):
        return None

    # 解析数值和单位
    if len(time_str) < 2:
        raise ValueError(f"无效的时间格式: {time_str}")

    try:
        value = int(time_str[:-1])
        unit = time_str[-1]
    except ValueError:
        raise ValueError(f"无效的时间格式: {time_str}")

    # 转换为秒
    if unit == "m":  # 分钟
        return value * 60
    elif unit == "h":  # 小时
        return value * 3600
    elif unit == "d":  # 天
        return value * 86400
    else:
        raise ValueError(f"不支持的时间单位: {unit}")


def validate_user_id(user_id: int) -> bool:
    """验证 Telegram 用户 ID 是否有效

    Args:
        user_id: 用户 ID

    Returns:
        是否有效
    """
    # Telegram 用户 ID 必须是正整数
    # 合理范围: 1 到 2^53-1 (JavaScript 安全整数范围)
    if not isinstance(user_id, int):
        return False

    return 0 < user_id < 2**53


async def auto_delete_message(message, delay: int = 30) -> None:
    """自动删除消息（延迟指定秒数后删除）

    Args:
        message: aiogram Message 对象
        delay: 延迟时间（秒），默认30秒

    注意：
        - 此函数会创建一个异步任务在后台执行
        - 如果消息已被删除或权限不足，会静默失败
    """
    async def _delete():
        try:
            await asyncio.sleep(delay)
            await message.delete()
            logger.debug(
                f"自动删除消息 [群组:{message.chat.id}] [消息ID:{message.message_id}]"
            )
        except Exception as e:
            # 静默处理删除失败（消息可能已被手动删除或权限不足）
            logger.debug(
                f"自动删除消息失败 [群组:{message.chat.id}] [消息ID:{message.message_id}]: {e}"
            )

    # 创建后台任务
    asyncio.create_task(_delete())

