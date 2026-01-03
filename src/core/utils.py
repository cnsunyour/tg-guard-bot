"""通用工具函数模块"""

import html
from typing import Optional


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

