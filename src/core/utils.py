"""通用工具函数模块"""

import asyncio
import html
import importlib.metadata
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner
from loguru import logger

from src.core.redis import RedisKeys, get_redis
from src.core.retry import retry_on_network_error


def _read_version_from_pyproject(pyproject_path: Path) -> str | None:
    """从 pyproject.toml 读取版本号，失败返回 None（交由调用方回退）

    - 校验 ``[project].name == "tg-guard-bot"``，避免读到同名异属的
      ``pyproject.toml``（如 monorepo、site-packages 残留）而误报版本
    - 文件不存在视为正常降级（纯 wheel 部署无 pyproject），静默返回 None；
      其余读取 / 解析异常记 WARNING 后返回 None
    """
    try:
        with pyproject_path.open("rb") as pyproject_file:
            data = tomllib.load(pyproject_file)
    except FileNotFoundError:
        return None  # 文件不存在属正常场景（如纯 wheel 部署），静默降级
    except (OSError, ValueError) as e:  # ValueError 已涵盖 TOMLDecodeError
        logger.warning(f"读取 {pyproject_path} 失败，将回退到包元数据: {e}")
        return None

    project = data.get("project")
    if not isinstance(project, dict) or project.get("name") != "tg-guard-bot":
        logger.warning(f"{pyproject_path} 非 tg-guard-bot 项目，将回退到包元数据")
        return None

    version = project.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()

    logger.warning(f"{pyproject_path} 缺少有效的 [project].version，将回退到包元数据")
    return None


def get_app_version() -> str:
    """获取应用版本号，用于 Sentry release 等场景

    版本的唯一来源是 ``pyproject.toml``。优先直接读取项目根目录的
    ``pyproject.toml``（源码直接运行时可即时反映版本号变更，无需重新安装）；
    当运行环境不含该文件（如纯 wheel 部署）时，回退读取已安装发行包的元数据。
    两者都失败则返回 ``"unknown"``，确保版本探测失败不影响应用启动。

    注意：容器环境中能否即时跟随版本号，取决于 ``pyproject.toml`` 是否随源码
    挂载或随镜像重建（本项目生产 Dockerfile 已 ``COPY pyproject.toml``）。

    Returns:
        应用版本号字符串；无法确定时返回 ``"unknown"``
    """
    # 1. 优先读取项目根目录的 pyproject.toml（源码直接运行时即时同步版本号）
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    version = _read_version_from_pyproject(pyproject_path)
    if version:
        return version

    # 2. 回退：读取已安装包的元数据（纯 wheel 部署等无 pyproject 的场景）
    try:
        version = importlib.metadata.version("tg-guard-bot").strip()
        if version:
            return version
        logger.warning("tg-guard-bot 包元数据版本为空，使用 unknown 占位")
    except importlib.metadata.PackageNotFoundError:
        logger.warning("未找到 tg-guard-bot 包元数据，使用 unknown 占位")
    except Exception as e:
        logger.warning(f"读取已安装包版本失败，使用 unknown 占位: {e}")

    return "unknown"


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


async def check_admin_permission_by_id(bot, chat_id: int, user_id: int) -> bool:
    """按用户 ID 检查管理员权限（超管直通 + 缓存 API 查询）。

    注意：此函数无 Message.sender_chat，**无法识别匿名管理员**——仅在确定有
    真实/代表 user_id 时使用（如 callback_query）。message 路径请用
    check_admin_permission(message, bot)。
    """
    from src.core.cache import PermissionCache
    from src.core.config import settings

    if user_id in settings.admin_ids:
        return True

    return await PermissionCache.is_admin(bot, chat_id, user_id)


async def check_admin_permission(message, bot) -> bool:
    """检查是否是管理员（统一的权限检查函数，message 版本）。

    支持：
    - 匿名管理员（sender_chat.id == chat.id）
    - 超级管理员（配置在 .env）
    - 群组管理员（通过 Telegram API 查询）

    Args:
        message: aiogram Message 对象
        bot: aiogram Bot 对象

    Returns:
        是否是管理员

    ✅ P1-10: 使用 Redis 缓存减少 API 调用
    """
    # fail-closed：无真实发送者时拒绝（channel post / 服务消息即使 sender_chat==chat.id 也不算管理员）
    if message.from_user is None:
        return False

    # 1. 检查是否是匿名管理员
    # 当管理员以"匿名管理员"身份执行命令时，sender_chat 会被设置为群组本身
    if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
        # ✅ 安全修复：脱敏处理命令内容，避免日志泄露敏感信息
        masked_text = mask_sensitive_text(message.text, keep_chars=8)
        logger.debug(f"匿名管理员执行命令 [群组:{message.chat.id}] [命令:{masked_text}]")
        return True

    # 2. 超管直通 + 缓存查询群组管理员（委托 by_id 避免重复逻辑）
    return await check_admin_permission_by_id(bot, message.chat.id, message.from_user.id)


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


def mask_user_name(name: str | None) -> str:
    """脱敏用户显示名，防止 spammer 借用户名投递广告

    规则：保留首尾各 1 个字符（共 2 个），中间用 ``*`` 替换；名字过短时
    降级为全部遮盖，保证脱敏后中间至少有一个 ``*``，避免短名字原样显示
    而绕过脱敏。

    - 长度 0~2：全部替换为 ``*``
    - 长度 ≥3：保留首尾各 1 个字符，其余替换为 ``*``

    按 Unicode code point 计数与切片。组合 emoji（ZWJ 序列、旗帜等）可能
    被从中间切开导致显示异常，但不影响广告文字的遮盖效果，故不引入额外
    依赖做字形簇感知。

    本函数只做脱敏、不做 HTML 转义；调用方需按「先脱敏后转义」顺序使用，
    即 ``escape_html(mask_user_name(...))``，避免破坏 HTML 实体。

    Example:
        >>> mask_user_name("张三")
        '**'
        >>> mask_user_name("张三李")
        '张*李'
        >>> mask_user_name("张三李四")
        '张**四'
        >>> mask_user_name("加微信低价VPN办理")
        '加********理'
    """
    if not name:
        return ""

    # 规范化空白：合并连续空白为单个空格并去除首尾空白，防止换行符破坏群消息布局
    normalized = " ".join(str(name).split())
    length = len(normalized)
    if length <= 1:
        return "*" * length

    # 首尾各保留 1 个字符，且保证中间至少留 1 个脱敏字符（length <= 2 时全遮）
    keep = min(1, (length - 1) // 2)
    if keep == 0:
        return "*" * length
    return normalized[:keep] + "*" * (length - keep * 2) + normalized[-keep:]


def format_user_mention(user) -> str:
    """安全地格式化用户提及，防止 HTML 注入与用户名广告投递

    显示名与 @username 均经 :func:`mask_user_name` 脱敏，避免 spammer 通过
    用户名展示广告。HTML 特殊字符在脱敏后再转义。

    Args:
        user: Telegram User 对象

    Returns:
        安全的用户提及字符串，形如 ``脱敏名 (@脱敏username)``
        或 ``脱敏名 (ID:用户ID)``
    """
    # 先脱敏再转义（顺序不可颠倒，否则会破坏 HTML 实体）
    name = escape_html(mask_user_name(user.full_name or user.first_name or "Unknown"))

    # @username 同样可能携带广告，一并脱敏；无 username 时回退到数字 ID（无需脱敏）
    identifier = (
        f"@{escape_html(mask_user_name(user.username))}" if user.username else f"ID:{user.id}"
    )

    return f"{name} ({identifier})"


def format_trusted_user_mention(user) -> str:
    """格式化可信用户（管理员/操作者）的纯文本提及，完整显示名称

    与 :func:`format_user_mention` 相反，本函数**不脱敏**显示名与
    @username，适用于已通过权限守卫确认可信的管理员/操作者/邀请者。
    显示名与 @username 仍统一经过 :func:`escape_html`，防止 HTML 注入
    及名称内的格式破坏字符。

    保留独立 API 而不向 :func:`format_user_mention` 添加 ``mask`` 开关，
    使所有「不脱敏」调用路径可被检索审计，避免误传开关导致名称泄露。

    Args:
        user: 已确认可信的 Telegram User 对象（管理员/操作者/邀请者）

    Returns:
        完整用户提及字符串，形如 ``显示名 (@username)``
        或 ``显示名 (ID:用户ID)``
    """
    name = escape_html(user.full_name or user.first_name or "Unknown")
    identifier = f"@{escape_html(user.username)}" if user.username else f"ID:{user.id}"
    return f"{name} ({identifier})"


def masked_mention_html(user) -> str:
    """生成显示名脱敏的可点击 HTML 用户提及

    与 :func:`format_user_mention` 不同，本函数生成可点击的 ``<a>`` 链接
    （基于可信的数字 user_id），管理员点击仍能精确定位用户，而链接文本
    只展示脱敏后的显示名，不暴露 spammer 塞入用户名的广告内容。

    Args:
        user: Telegram User 对象

    Returns:
        HTML mention 字符串，如 ``<a href="tg://user?id=123">张***三</a>``

    Note:
        点击跳转后，Telegram 资料页仍会展示真实名称，属于平台行为，
        Bot 端无法屏蔽。
    """
    name = escape_html(mask_user_name(user.full_name or user.first_name or "Unknown"))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


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


# Telegram 系统服务账号（固定 ID，无需检测也不可封禁）
# 777000: Telegram 服务通知（登录验证码、关联频道同步转发、官方公告等）
TELEGRAM_SERVICE_IDS: frozenset[int] = frozenset({777000})


def should_skip_sender(user_id: int, bot_id: int) -> bool:
    """判断消息发送者是否应优先跳过反垃圾 / 用户状态检测

    覆盖：
    - Telegram 系统服务账号（如 777000，关联频道同步、服务通知）
    - Bot 自身（user_id == bot_id，避免消息回环触发自检）

    用于 CAS 检查中间件、频道马甲检测、活跃度限制等统一短路，
    避免对特殊来源发起无谓的外部 API 请求。

    Args:
        user_id: 发送者用户 ID
        bot_id: 当前 Bot 的用户 ID

    Returns:
        True 表示该发送者应跳过后续检测
    """
    if user_id in TELEGRAM_SERVICE_IDS:
        return True
    return user_id == bot_id


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


def parse_message_link_with_chat(text: str) -> tuple[int | None, int | None, str | None]:
    """从消息链接中提取群组标识和消息ID

    支持的格式：
    1. 纯数字：123456 -> (None, 123456, None)
    2. 私有群组链接：https://t.me/c/1234567890/123456 -> (-1001234567890, 123456, None)
    3. 公开频道/群组链接：https://t.me/channel_name/123456 -> (None, 123456, "channel_name")

    Args:
        text: 消息链接或消息ID字符串

    Returns:
        (chat_id, message_id, username) 元组
        - 私有群组：返回完整的 chat_id (带 -100 前缀)，username 为 None
        - 公开群组：返回 username，chat_id 为 None
        - 纯数字：chat_id 和 username 都为 None
        - 解析失败：(None, None, None)
    """
    if not text or not isinstance(text, str):
        return None, None, None

    text = text.strip()

    # 1. 尝试直接解析为数字
    if text.isdigit():
        message_id = int(text)
        if message_id > 0:
            return None, message_id, None
        return None, None, None

    # 2. 解析私有群组链接：https://t.me/c/1234567890/123456
    # 提取群组ID和消息ID
    private_pattern = r"t\.me/c/(\d+)/(\d+)"
    match = re.search(private_pattern, text)
    if match:
        # 提取链接中的群组ID（不带前缀）
        chat_id_str = match.group(1)
        message_id_str = match.group(2)

        try:
            # 转换为完整的 chat_id（加上 -100 前缀）
            # Telegram 私有群组的 chat_id 格式：-100 + 链接中的数字
            # 例如：链接中是 1234567890，实际 chat_id 是 -1001234567890
            chat_id = int(f"-100{chat_id_str}")
            message_id = int(message_id_str)

            if message_id > 0:
                logger.debug(f"从私有群组链接解析: chat_id={chat_id}, message_id={message_id}")
                return chat_id, message_id, None
        except (ValueError, OverflowError) as e:
            logger.debug(f"解析私有群组链接失败: {e}")
            return None, None, None

    # 3. 解析公开频道/群组链接：https://t.me/channel_name/123456
    # 提取 username 和消息ID
    public_pattern = r"t\.me/([^/]+)/(\d+)"
    match = re.search(public_pattern, text)
    if match:
        try:
            username = match.group(1)
            message_id = int(match.group(2))

            # 排除特殊路径（如 /c/, /s/ 等）
            if username not in ["c", "s", "addstickers", "joinchat", "login"]:
                if message_id > 0:
                    logger.debug(f"从公开链接解析: username={username}, message_id={message_id}")
                    return None, message_id, username
        except ValueError:
            pass

    # 解析失败
    masked_text = mask_sensitive_text(text, keep_chars=15)
    logger.debug(f"无法从文本中解析消息链接: {masked_text}")
    return None, None, None


@retry_on_network_error(max_retries=3, initial_delay=1.0)
async def get_chat_administrators_mention(
    bot: Bot,
    chat_id: int,
) -> str:
    """获取群组管理员列表的 mention 字符串

    使用 user_id 方式生成 mention，适用于所有管理员（包括没有 username 的）
    结果会被缓存 5 分钟，减少 Telegram API 调用

    Args:
        bot: Bot 实例
        chat_id: 群组 ID

    Returns:
        mention 字符串，包含所有非匿名管理员，空格分隔
        每个管理员显示为 👤 emoji，不会显示真实姓名

    Example:
        >>> mentions = await get_chat_administrators_mention(bot, chat_id)
        >>> print(mentions)
        '<a href="tg://user?id=123">👤</a> <a href="tg://user?id=456">👤</a>'
    """
    redis = get_redis()
    cache_key = RedisKeys.chat_admins(chat_id)

    # 1. 尝试从 Redis 缓存获取
    cached_data = await redis.get(cache_key)
    if cached_data:
        try:
            admins: list[dict[str, Any]] = json.loads(cached_data)
            mentions = " ".join(f'<a href="tg://user?id={admin["id"]}">👤</a>' for admin in admins)
            logger.debug(f"从缓存获取管理员列表 [群组:{chat_id}] [数量:{len(admins)}]")
            return mentions
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"解析管理员缓存失败 [群组:{chat_id}]: {e}")

    # 2. 从 Telegram API 获取
    try:
        administrators = await bot.get_chat_administrators(chat_id)

        # 3. 过滤匿名管理员（无法 mention）
        # 仅保留有用户信息的非匿名管理员
        non_anonymous_admins = [
            admin
            for admin in administrators
            if not getattr(admin, "is_anonymous", False)
            and admin.user
            and isinstance(admin, (ChatMemberOwner, ChatMemberAdministrator))
        ]

        if not non_anonymous_admins:
            logger.debug(f"群组没有非匿名管理员 [群组:{chat_id}]")
            return ""

        # 4. 构建缓存数据（只存 ID）
        admins_data = [{"id": admin.user.id} for admin in non_anonymous_admins]

        # 5. 缓存结果（5分钟 TTL）
        await redis.setex(cache_key, 300, json.dumps(admins_data, ensure_ascii=False))

        # 6. 构建 mention 字符串（使用 emoji 代替显示名称）
        mentions = " ".join(f'<a href="tg://user?id={admin["id"]}">👤</a>' for admin in admins_data)

        logger.debug(f"获取管理员列表 [群组:{chat_id}] [数量:{len(admins_data)}] [已缓存]")
        return mentions

    except Exception as e:
        logger.error(f"获取管理员列表失败 [群组:{chat_id}]: {e}")
        return ""


def calculate_normalized_length(text: str | None) -> int:
    """计算标准化文本长度（中英文公平对待）

    计算规则：
    - 1个汉字/全角字符 = 1标准长度
    - 2个英文字符 = 1标准长度（半角字符0.5，2个半角=1标准长度）

    示例：
    - "你好世界" → 4标准长度
    - "Hello World" → 5.5标准长度（11字符 × 0.5）
    - "加微信xxx" → 5标准长度
    - "Add me: xxx" → 5.5标准长度（11字符 × 0.5）

    Args:
        text: 待计算的文本

    Returns:
        标准化长度（整数）
    """
    if not text:
        return 0

    normalized_length = 0.0

    for char in text:
        # 判断是否为汉字或全角字符
        # 汉字Unicode范围：\u4e00-\u9fff
        # 全角字符：ord > 127
        if ("\u4e00" <= char <= "\u9fff") or ord(char) > 127:
            normalized_length += 1.0
        else:
            normalized_length += 0.5

    return int(normalized_length)
