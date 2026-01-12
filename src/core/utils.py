"""通用工具函数模块"""

import asyncio
import html
import re

from loguru import logger

from src.core.retry import retry_on_network_error


def mask_sensitive_text(text: str | None, keep_chars: int = 10) -> str:
    """脱敏处理敏感文本，用于日志记录

    Args:
        text: 待脱敏的文本
        keep_chars: 保留前后各多少个字符

    Returns:
        脱敏后的文本

    Example:
        >>> mask_sensitive_text("这是一条敏感消息内容", keep_chars=4)
        "这是一条...息内容"
        >>> mask_sensitive_text("短", keep_chars=10)
        "***"
    """
    if not text:
        return "***"

    text_str = str(text)

    # 如果文本很短，完全脱敏
    if len(text_str) <= keep_chars * 2:
        return "***"

    # 保留前后各 keep_chars 个字符
    return f"{text_str[:keep_chars]}...{text_str[-keep_chars:]}"


async def check_admin_permission(message, bot) -> bool:
    """检查是否是管理员（统一的权限检查函数）

    支持：
    - 超级管理员（配置在 .env）
    - 群组管理员（通过 Telegram API 查询）
    - 匿名管理员（sender_chat.id == chat.id）

    Args:
        message: aiogram Message 对象
        bot: aiogram Bot 对象

    Returns:
        是否是管理员

    ✅ P1-10: 使用 Redis 缓存减少 API 调用
    """
    from src.core.cache import PermissionCache
    from src.core.config import settings

    # 1. 检查是否是匿名管理员
    # 当管理员以"匿名管理员"身份执行命令时，sender_chat 会被设置为群组本身
    if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
        # ✅ 安全修复：脱敏处理命令内容，避免日志泄露敏感信息
        masked_text = mask_sensitive_text(message.text, keep_chars=8)
        logger.debug(f"匿名管理员执行命令 [群组:{message.chat.id}] [命令:{masked_text}]")
        return True

    # 2. 检查是否在配置的超级管理员列表中
    if message.from_user.id in settings.admin_ids:
        return True

    # 3. 使用缓存检查是否是群组管理员
    return await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id)


@retry_on_network_error(max_retries=3, initial_delay=1.0)
async def check_admin_permission_strict(bot, chat_id: int, user_id: int) -> bool:
    """严格的管理员权限检查（不信任缓存，直接查询 API）

    用于关键操作（踢人、封禁、白名单等），防止 Redis 妥协导致的权限提升攻击

    Args:
        bot: aiogram Bot 对象
        chat_id: 群组 ID
        user_id: 用户 ID

    Returns:
        是否是管理员

    ✅ 安全加固：关键操作不信任 Redis 缓存
    """
    from src.core.config import settings

    # 1. 检查是否在配置的超级管理员列表中
    if user_id in settings.admin_ids:
        return True

    # 2. 直接查询 Telegram API（不使用缓存）
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        is_admin = member.status in ["creator", "administrator"]
        logger.debug(
            f"严格权限检查 [群组:{chat_id}] [用户:{user_id}] [结果:{is_admin}] [状态:{member.status}]"
        )
        return is_admin
    except Exception as e:
        logger.error(f"严格权限检查失败 [群组:{chat_id}] [用户:{user_id}]: {e}")
        return False


async def check_admin_permission_strict_message(message, bot) -> bool:
    """严格权限检查（Message 版本）

    - 兼容匿名管理员（sender_chat.id == chat.id）
    - 不使用 Redis 缓存，直接查询 Telegram API

    用于关键操作（踢人、封禁、白名单等），防止 Redis 妥协导致的权限提升攻击

    Args:
        message: aiogram Message 对象
        bot: aiogram Bot 对象

    Returns:
        是否是管理员
    """
    # 支持匿名管理员
    if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
        masked_text = mask_sensitive_text(getattr(message, "text", None), keep_chars=8)
        logger.debug(f"匿名管理员执行命令 [群组:{message.chat.id}] [命令:{masked_text}]")
        return True

    # 检查是否有 from_user
    if not getattr(message, "from_user", None):
        return False

    return await check_admin_permission_strict(bot, message.chat.id, message.from_user.id)


def escape_html(text: str | None) -> str:
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
    identifier = f"@{user.username}" if user.username else f"ID:{user.id}"

    return f"{name} ({identifier})"


def mask_text(text: str | None, show_length: int = 10) -> str:
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


def parse_time_to_seconds(time_str: str) -> int | None:
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
            logger.debug(f"自动删除消息 [群组:{message.chat.id}] [消息ID:{message.message_id}]")
        except Exception as e:
            # 静默处理删除失败（消息可能已被手动删除或权限不足）
            logger.debug(
                f"自动删除消息失败 [群组:{message.chat.id}] [消息ID:{message.message_id}]: {e}"
            )

    # 创建后台任务
    asyncio.create_task(_delete())


def parse_message_link(text: str) -> int | None:
    """从消息链接或消息ID中提取消息ID

    支持的格式：
    1. 纯数字：123456
    2. 公开频道/群组链接：https://t.me/channel_name/123456
    3. 私有群组链接：https://t.me/c/1234567890/123456
    4. 带参数的链接：https://t.me/c/1234567890/123456?comment=789

    Args:
        text: 消息链接或消息ID字符串

    Returns:
        消息ID，如果解析失败返回 None
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()

    # 1. 尝试直接解析为数字
    if text.isdigit():
        message_id = int(text)
        if message_id > 0:
            return message_id
        return None

    # 2. 解析 Telegram 消息链接
    # 支持格式：
    # - https://t.me/channel_name/123456
    # - https://t.me/c/1234567890/123456
    # - t.me/channel_name/123456 (无协议)

    # 匹配模式：提取最后的数字部分（消息ID）
    # 模式说明：
    # - t\.me/ : 匹配域名
    # - (?:c/\d+/)? : 可选的私有群组ID部分
    # - [^/]+/ : 频道名称或其他路径
    # - (\d+) : 消息ID（捕获组）
    # - (?:\?|$) : 后面跟查询参数或结束

    patterns = [
        r"t\.me/c/\d+/(\d+)",  # 私有群组：t.me/c/chat_id/message_id
        r"t\.me/[^/]+/(\d+)",  # 公开频道/群组：t.me/channel/message_id
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            message_id = int(match.group(1))
            if message_id > 0:
                logger.debug(f"从链接中解析到消息ID: {message_id}")
                return message_id

    # 解析失败
    # ✅ 安全修复：脱敏处理文本内容，避免日志泄露敏感信息
    masked_text = mask_sensitive_text(text, keep_chars=15)
    logger.debug(f"无法从文本中解析消息ID: {masked_text}")
    return None
