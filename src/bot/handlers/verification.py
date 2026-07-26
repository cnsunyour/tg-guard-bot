"""入群验证处理器"""

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.methods.decline_chat_join_request import DeclineChatJoinRequest
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyKeyboardRemove,
)
from loguru import logger

from src.bot.handlers.verification_render import (
    VerificationFlow,
    render_captcha_for_refresh,
    render_verification_challenge,
)
from src.core.cache import PermissionCache
from src.core.config import settings
from src.core.i18n import get_resolver, get_translator
from src.core.redis import RedisKeys, get_redis
from src.core.retry import retry_async_call
from src.core.utils import (
    auto_delete_message,
    escape_html,
    format_trusted_user_mention,
    format_user_mention,
    masked_mention_html,
)
from src.repositories.audit_repo import AuditRepository
from src.repositories.group_repo import GroupRepository
from src.services.cas_service import get_cas_service
from src.services.spam_detector import SpamDetector
from src.services.user_status_service import get_user_status_service
from src.services.username_mapping import UsernameMappingService
from src.services.verification import VerificationChallenge, VerificationService

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

router = Router(name="verification")

# 安全释放 in-flight 锁的 Lua 脚本：仅当键值等于 owner token 时才删除，
# 避免「单次处理耗时超过 TTL → 旧协程 finally 误删新协程刚取得的锁」。
_INFLIGHT_RELEASE_SCRIPT = (
    'if redis.call("get", KEYS[1]) == ARGV[1] then '
    'return redis.call("del", KEYS[1]) '
    "end "
    "return 0"
)


@contextlib.asynccontextmanager
async def _verification_inflight_lock(lock_key: str) -> AsyncIterator[bool]:
    """获取带 owner token 校验的 Redis in-flight 锁。

    用 ``SET NX EX`` 取锁，随机 token 作为值；离开上下文时用 Lua compare-and-delete
    释放，确保只删除自己持有的锁。TTL 由 ``settings.verification_inflight_ttl_seconds``
    控制，仅作进程异常退出时的死锁兜底，正常路径在 yield 结束后立即释放。

    Yields:
        是否成功取得锁；为 ``False`` 表示已有处理在进行，调用方应直接返回。
    """
    redis = get_redis()
    lock_token = secrets.token_hex(16)
    acquired = bool(
        await redis.set(
            lock_key,
            lock_token,
            nx=True,
            ex=settings.verification_inflight_ttl_seconds,
        )
    )
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await redis.eval(_INFLIGHT_RELEASE_SCRIPT, 1, lock_key, lock_token)


async def decline_join_request(bot: Bot, chat_id: int, user_id: int) -> bool:
    """拒绝加入请求（修复 HIDE_REQUESTER_MISSING 错误）

    Telegram Bot API 6.3+ 引入了 hide_requester 参数，
    但 aiogram 3.24.0 的封装方法尚未暴露该参数。

    Args:
        bot: Bot 实例
        chat_id: 群组 ID
        user_id: 用户 ID

    Returns:
        是否成功拒绝
    """
    try:
        call = DeclineChatJoinRequest(
            chat_id=chat_id,
            user_id=user_id,
            hide_requester=False,  # 显示是 Bot 拒绝的请求
        )
        return await bot(call)
    except Exception as e:
        logger.debug(f"拒绝加入请求失败: {e}")
        raise


async def _restore_user_permissions_once(bot: Bot, chat_id: int, user_id: int) -> None:
    """单次恢复用户权限的核心操作（不含异常兜底）。

    幂等：每次调用都重新查询会员状态并按需恢复，可安全重试。
    由 :func:`restore_user_permissions` 通过 ``retry_async_call`` 包裹重试。
    """
    member = await bot.get_chat_member(chat_id, user_id)

    if member.status == "kicked":
        # 用户被封禁，解除封禁（only_if_banned 避免 restricted 用户被误踢）
        await bot.unban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            only_if_banned=True,
        )
    elif member.status == "restricted":
        # 恢复权限 + 31 秒后自动移出 restricted 列表
        until_date = datetime.utcnow() + timedelta(seconds=31)

        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
                can_manage_topics=False,
            ),
            until_date=until_date,
        )
    # 其他状态（member, administrator 等）无需操作

    logger.info(f"已恢复用户 {user_id} 权限 (原状态: {member.status})")


async def restore_user_permissions(bot: Bot, chat_id: int, user_id: int) -> bool:
    """恢复用户权限并从 restricted 列表移除（30秒后自动移除）。

    内部对 Telegram 网络临时错误自动重试（最多 3 次，指数退避）；重试耗尽
    或发生非网络错误时返回 ``False``，由调用方降级处理。

    Returns:
        是否成功
    """
    try:
        await retry_async_call(lambda: _restore_user_permissions_once(bot, chat_id, user_id))
        return True
    except Exception as e:
        logger.error(f"恢复用户权限失败（重试耗尽）[用户:{user_id}]: {e}")
        return False


async def approve_join_request(bot: Bot, chat_id: int, user_id: int) -> bool:
    """批准用户的加入请求。

    内部对 Telegram 网络临时错误自动重试（最多 3 次，指数退避）；重试耗尽
    或发生非网络错误时返回 ``False``，由调用方降级处理。

    Returns:
        是否成功
    """
    try:
        await retry_async_call(
            lambda: bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        )
        return True
    except Exception as e:
        logger.error(f"批准加入请求失败（重试耗尽）[用户:{user_id}]: {e}")
        return False


async def send_verification_success_message(
    bot: Bot,
    user_id: int,
    chat_title: str,
    message_type: str = "success",
    *,
    group_chat_id: int | None = None,
) -> None:
    """发送验证结果消息（纯文本）。

    所有关键操作（权限恢复、加入请求批准）均由调用方带重试执行，失败时传入
    对应的 ``*_failed`` 类型，由本函数发送降级引导文案。不再创建邀请链接：
    restricted 用户点击「加入群组」链接无效，重试 + 联系管理员更可靠。

    Args:
        bot: Bot 实例
        user_id: 用户 ID
        chat_title: 群组标题（已转义 HTML）
        message_type: 消息类型
            - "success": 验证成功，已恢复群组权限
            - "success_join_request": 验证成功，加入请求已批准
            - "restore_failed": 验证已通过，但权限恢复失败（用户仍在群内）
            - "approve_failed": 验证已通过，但加入请求批准失败（用户尚未入群）
        group_chat_id: 来源群 ID。传入时按「用户偏好 → 来源群 locale」渲染
            （math 验证）；不传则保留中文文案（其他验证类型 POC 阶段）。
    """
    if group_chat_id is not None:
        locale = await get_resolver().for_private_from_group(
            user_id=user_id, group_chat_id=group_chat_id
        )
        localizer = get_translator().for_locale(locale)
        text = localizer.t(
            f"verification.success.private.{message_type}.message",
            chat_title=chat_title,
        )
    elif message_type == "success":
        text = f"✅ <b>验证成功！</b>\n\n您已成功加入群组：<b>{chat_title}</b>\n\n现在可以在群内自由发言了！"
    elif message_type == "success_join_request":
        text = f"✅ <b>验证成功！</b>\n\n您的加入请求已批准，正在加入群组：<b>{chat_title}</b>\n\n稍后您将能在群内自由发言！"
    elif message_type == "restore_failed":
        text = (
            f"✅ <b>验证已通过</b>\n\n暂时无法自动恢复您在群组 <b>{chat_title}</b> 中的发言权限。\n\n"
            f"请稍后在群内尝试发言；若仍无法发言，请联系管理员协助处理。"
        )
    elif message_type == "approve_failed":
        text = (
            f"✅ <b>验证已通过</b>\n\n暂时无法自动批准您加入群组：<b>{chat_title}</b>。\n\n"
            f"请稍后重新提交加入请求；若仍无法加入，请联系管理员协助处理。"
        )
    else:
        text = f"✅ <b>验证成功！</b>\n\n群组：<b>{chat_title}</b>"

    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


async def check_user_spam_info(
    bot: Bot, chat_id: int, user_id: int, username: str, mode: str = "join"
) -> bool:
    """检测用户信息是否为垃圾

    Args:
        bot: Bot 实例
        chat_id: 群组 ID
        user_id: 用户 ID
        username: 用户名
        mode: 处理模式，"join" 或 "join_request"

    Returns:
        True: 检测到垃圾信息并已处理
        False: 通过检测，继续正常流程
    """
    try:
        user_info = await bot.get_chat(user_id)

        # 构建检测文本：名字 + bio
        check_texts = []
        if user_info.first_name:
            check_texts.append(user_info.first_name)
        if user_info.last_name:
            check_texts.append(user_info.last_name)
        if hasattr(user_info, "bio") and user_info.bio:
            check_texts.append(user_info.bio)

        if not check_texts:
            return False

        check_text = " ".join(check_texts)

        # 使用反垃圾检测器检测
        detector = SpamDetector()
        result = await detector.detect_with_ai(
            text=check_text,
            user_id=user_id,
            chat_id=chat_id,
            skip_auto_train=True,
        )

        if result["is_spam"]:
            mode_text = "加入请求用户" if mode == "join_request" else "用户"
            logger.warning(
                f"{mode_text} {username} ({user_id}) 信息疑似垃圾，拒绝入群并封禁 1 小时\n"
                f"检测内容: {check_text[:100]}...\n"  # 只记录前 100 字符，保护隐私
                f"置信度: {result['confidence']:.2f}\n"
                f"原因: {', '.join(result.get('reasons', ['未知']))}"
            )

            # 处理加入请求模式：先拒绝加入请求
            if mode == "join_request":
                try:
                    await decline_join_request(bot, chat_id, user_id)
                    logger.info(f"已拒绝垃圾用户 {user_id} 的加入请求")
                except Exception as decline_error:
                    logger.error(f"拒绝加入请求失败: {decline_error}")

                # ✅ 清除验证状态，避免 timeout 任务重复处理（修复 HIDE_REQUESTER_MISSING）
                verification_service = VerificationService()
                await verification_service.clear_verification(chat_id, user_id)

            # 封禁 1 小时（两种模式通用）
            try:
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=datetime.now() + timedelta(hours=1),
                )
                logger.info(f"已封禁垃圾用户 {user_id} 1 小时")
            except Exception as ban_error:
                logger.error(f"封禁用户失败: {ban_error}")

            return True  # 已检测到垃圾并处理

    except Exception as e:
        logger.error(f"检测用户信息失败: {e}")
        return False  # 检测失败，继续正常流程

    return False  # 通过检测


async def generate_verification_challenge(
    group, chat_id: int, user_id: int
) -> VerificationChallenge:
    """根据群组配置生成验证挑战

    Args:
        group: 群组配置对象
        chat_id: 群组 ID
        user_id: 用户 ID

    Returns:
        验证挑战对象（discriminated union）
    """
    verification_service = VerificationService()
    timeout = group.verification_timeout

    generators: dict[str, Callable[[int, int, int], Awaitable[VerificationChallenge]]] = {
        "math": verification_service.generate_math_challenge,
        "slider": verification_service.generate_slider_challenge,
        "qa": verification_service.generate_qa_challenge,
        "emoji": verification_service.generate_emoji_challenge,
        "captcha": verification_service.generate_captcha_challenge,
        "honeypot": verification_service.generate_honeypot_challenge,
        "puzzle": verification_service.generate_puzzle_challenge,
        "turnstile": verification_service.generate_turnstile_challenge,
        "friendly": verification_service.generate_friendly_challenge,
        "hcaptcha": verification_service.generate_hcaptcha_challenge,
        "mtcaptcha": verification_service.generate_mtcaptcha_challenge,
        "altcha": verification_service.generate_altcha_challenge,
        "random": verification_service.generate_random_challenge,
    }
    # 未知类型默认数学验证
    generator = generators.get(
        group.verification_type, verification_service.generate_math_challenge
    )
    return await generator(chat_id, user_id, timeout)


async def send_verification_message(
    bot: Bot,
    chat_id: int,
    user_id: int,
    challenge: VerificationChallenge,
    flow: VerificationFlow,
    username: str,
    timeout: int,
):
    """发送验证消息到用户私聊

    按「用户偏好 → 来源群 locale」解析语言，交由 render 层渲染文案与 keyboard。

    Args:
        bot: Bot 实例
        chat_id: 群组 ID
        user_id: 用户 ID
        challenge: 验证挑战对象（discriminated union）
        flow: 验证流程，"join"（已入群）或 "join_request"（加入请求）
        username: 用户显示名（未转义，render 层统一 escape）
        timeout: 验证超时时间（秒）

    Returns:
        发送的消息对象
    """
    # 获取群组信息（chat_title 传原始文本，render 层统一 escape_html）
    chat = await bot.get_chat(chat_id)
    chat_title = chat.title or "群组"

    # 私聊验证消息按「用户偏好 → 来源群 locale」渲染
    locale = await get_resolver().for_private_from_group(user_id=user_id, group_chat_id=chat_id)
    localizer = get_translator().for_locale(locale)
    rendered = render_verification_challenge(
        challenge,
        localizer,
        chat_id=chat_id,
        user_id=user_id,
        flow=flow,
        timeout=timeout,
        username=username,
        chat_title=chat_title,
    )

    # 根据是否有图片选择发送方式
    if rendered.photo is not None:
        # captcha / puzzle 验证：发送图片
        return await bot.send_photo(
            chat_id=user_id,
            photo=rendered.photo,
            caption=rendered.text,
            reply_markup=rendered.keyboard,
            parse_mode="HTML",
        )
    # 其他验证：发送文本消息
    return await bot.send_message(
        chat_id=user_id,
        text=rendered.text,
        reply_markup=rendered.keyboard,
        parse_mode="HTML",
    )


@router.chat_join_request()
async def on_join_request(event: ChatJoinRequest, bot: Bot) -> None:
    """处理加入请求事件 - 启用 Approve New Members 时触发

    验证通过后自动批准加入请求
    """
    chat_id = event.chat.id
    user = event.from_user
    user_id = user.id
    username = user.username or user.full_name

    logger.info(f"收到加入请求: 用户 {username} ({user_id}) 请求加入群组 {chat_id}")

    # 加入请求去重：1分钟内同一用户同一群组只处理一次
    redis = get_redis()
    dedup_key = RedisKeys.join_request_dedup(chat_id, user_id)
    if not await redis.set(dedup_key, "1", nx=True, ex=60):
        logger.debug(f"用户 {user_id} 在群组 {chat_id} 的加入请求已被处理，跳过重复请求")
        return

    # 记录 username 映射
    if user.username:
        await UsernameMappingService.update_mapping(
            user_id=user_id,
            username=user.username,
        )

    # 处理中互斥锁：覆盖 CAS/Telethon 状态/AI 等慢检测整段窗口（耗时可能远超 60 秒
    # dedup），防止用户在 dedup 过期后重复点击触发重复 AI 请求。锁在离开上下文时
    # 立即释放（owner token 校验），TTL 仅作异常兜底。
    async with _verification_inflight_lock(
        RedisKeys.join_request_inflight(chat_id, user_id)
    ) as lock_acquired:
        if not lock_acquired:
            logger.debug(f"用户 {user_id} 在群组 {chat_id} 的加入请求仍在处理中，跳过重复请求")
            return

        # 异常处理保持与重构前一致：前置阶段（CAS/状态/AI）的未预期异常向上冒泡
        # 至全局错误处理器（记录堆栈 + Sentry 上报），仅 group/challenge/发送阶段
        # 的异常在 _process_join_request 内部捕获。锁始终由 async with 的 finally 释放。
        await _process_join_request(event, bot, chat_id, user_id, username)


async def _handle_approved_join_request(bot: Bot, chat_id: int, user_id: int) -> None:
    """处理已通过验证后重新提交的加入请求：直接批准并私聊通知。"""
    logger.info(f"用户 {user_id} 已通过验证，直接批准加入请求")

    chat_title = "群组"
    with contextlib.suppress(Exception):
        chat = await bot.get_chat(chat_id)
        chat_title = escape_html(chat.title) if chat.title else "群组"

    if await approve_join_request(bot, chat_id, user_id):
        logger.info(f"已批准用户 {user_id} 的加入请求（已验证用户）")
        # 不删除 approved_key：用户随后加入群组时由 on_user_join 恢复权限并消费此标记
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ <b>加入成功！</b>\n\n您的加入请求已批准，欢迎加入群组：<b>{chat_title}</b>\n\n现在可以在群内自由发言了！",
                parse_mode="HTML",
            )
        return

    # 批准失败（重试耗尽）：保留 approved_key 以便用户重新提交加入请求时自动批准
    with contextlib.suppress(Exception):
        await send_verification_success_message(bot, user_id, chat_title, "approve_failed")


async def _process_join_request(
    event: ChatJoinRequest,
    bot: Bot,
    chat_id: int,
    user_id: int,
    username: str,
) -> None:
    """执行已取得 in-flight 锁的加入请求处理流程。"""
    redis = get_redis()
    verification_service = VerificationService()

    # 快速路径：已有进行中的验证则直接返回，避免再次执行 CAS/状态/AI 检测
    if await verification_service.is_verification_pending(chat_id, user_id):
        logger.debug(
            f"用户 {user_id} 在群组 {chat_id} 已有进行中的验证（加入请求模式），忽略重复请求"
        )
        return

    # 已验证通过（如此前批准失败）则直接重试批准，跳过昂贵的前置检测
    if await redis.get(RedisKeys.verification_approved(chat_id, user_id)):
        await _handle_approved_join_request(bot, chat_id, user_id)
        return

    # ========== CAS 黑名单检查（优先级最高）==========
    if settings.cas_enabled:
        cas_service = get_cas_service()
        cas_result = await cas_service.check_user(user_id)

        if cas_result.is_banned:
            # 拒绝加入请求
            try:
                await decline_join_request(bot, chat_id, user_id)
            except Exception as e:
                logger.warning(f"拒绝 CAS 加入请求失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

            # 封禁用户（防止再次请求加入）
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            except Exception as e:
                logger.warning(f"CAS 封禁用户失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

            # 记录审计日志
            with contextlib.suppress(Exception):
                await AuditRepository.log_action(
                    group_id=chat_id,
                    operator_id=bot.id,
                    action="cas_ban_on_join_request",
                    target_user_id=user_id,
                    details={"offenses": cas_result.offenses},
                )

            logger.info(f"CAS 黑名单用户加入请求被拒 [群组:{chat_id}] [用户:{user_id}]")
            return  # 结束处理
    # ========== CAS 检查结束 ==========

    # ========== 用户状态检查（Telethon）==========
    if settings.user_status_check_enabled:
        status_service = get_user_status_service()
        status_result = await status_service.check_user(user_id, chat_id)

        if status_result.is_problematic:
            # 拒绝加入请求
            try:
                await decline_join_request(bot, chat_id, user_id)
            except Exception as e:
                logger.warning(f"拒绝异常用户加入请求失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

            # 封禁用户（防止再次请求加入）
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            except Exception as e:
                logger.warning(f"封禁异常用户失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

            # 记录审计日志
            with contextlib.suppress(Exception):
                await AuditRepository.log_action(
                    group_id=chat_id,
                    operator_id=bot.id,
                    action=f"user_status_ban_on_join_request_{status_result.reason}",
                    target_user_id=user_id,
                    details={"status": status_result.reason},
                )

            logger.info(
                f"异常用户加入请求被拒 [群组:{chat_id}] [用户:{user_id}] "
                f"[状态:{status_result.reason}]"
            )
            return  # 结束处理
    # ========== 用户状态检查结束 ==========

    # ==================== 用户信息反垃圾检测 ====================
    if await check_user_spam_info(bot, chat_id, user_id, username, mode="join_request"):
        return  # 检测到垃圾信息，已处理，直接返回
    # ==================== 用户信息反垃圾检测结束 ====================

    try:
        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id, event.chat.title)

        # 根据验证类型生成挑战
        challenge = await generate_verification_challenge(group, chat_id, user_id)

        # 尝试私聊发送验证消息
        try:
            # 标记验证类型为加入请求验证
            type_key = RedisKeys.verification_type(chat_id, user_id)
            await redis.setex(type_key, group.verification_timeout + 10, "join_request")

            # 发送验证消息
            sent_message = await send_verification_message(
                bot,
                chat_id,
                user_id,
                challenge,
                flow="join_request",
                username=username,
                timeout=group.verification_timeout,
            )

            logger.info(f"已向用户 {user_id} 私聊发送加入请求验证消息")

            # 启动超时处理任务（拒绝加入请求）
            asyncio.create_task(
                handle_join_request_timeout(
                    bot, chat_id, user_id, sent_message.message_id, group.verification_timeout
                )
            )

        except TelegramForbiddenError:
            # 用户未启动 Bot，使用共享引导消息机制，并拒绝加入请求
            logger.warning(f"用户 {user_id} 未启动 Bot，无法发送私聊验证，拒绝加入请求")
            await handle_user_not_started_bot_for_join_request(bot, chat_id, user_id)

        except Exception as e:
            logger.error(f"发送私聊验证消息失败: {e}")
            # 拒绝加入请求
            await decline_join_request(bot, chat_id, user_id)

            # ✅ 清除验证状态，避免 timeout 任务重复处理（修复 HIDE_REQUESTER_MISSING）
            await verification_service.clear_verification(chat_id, user_id)

    except Exception as e:
        logger.error(f"处理加入请求失败: {e}")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_user_join(event: ChatMemberUpdated, bot: Bot) -> None:
    """处理用户加入群组事件 - 私聊验证模式

    如果用户已通过加入请求验证，直接欢迎加入，跳过重复验证
    如果是管理员邀请进群，直接通过验证
    """
    chat_id = event.chat.id
    user = event.new_chat_member.user
    user_id = user.id
    username = user.username or user.full_name

    # ✅ 绝对第一步：写 joining 标记，覆盖 restrict 生效前的抢发窗口
    # 必须早于 restrict 与所有后续检查，确保 chat_member 更新一到就落盘；
    # 对所有入群者统一适用，靠 TTL 自动过期，不在任何路径显式清理。
    # Redis 抖动不应中断入群验证流程，故仅记录日志。
    try:
        await get_redis().setex(
            RedisKeys.verification_joining(chat_id, user_id),
            settings.verification_joining_window_seconds,
            "1",
        )
    except Exception as e:
        logger.error(f"写入入群短窗口标记失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

    logger.info(f"用户 {username} ({user_id}) 加入群组 {chat_id}")

    # ✅ 记录 username 映射
    if user.username:
        await UsernameMappingService.update_mapping(
            user_id=user_id,
            username=user.username,
        )

    # 处理中互斥锁：防止 chat_member 事件重复投递导致并发重入（覆盖 restrict/
    # CAS/状态/AI 整段窗口）。锁在离开上下文时立即释放。
    async with _verification_inflight_lock(
        RedisKeys.join_inflight(chat_id, user_id)
    ) as lock_acquired:
        if not lock_acquired:
            logger.debug(f"用户 {user_id} 在群组 {chat_id} 的入群事件仍在处理中，跳过重复事件")
            return

        # 异常处理保持与重构前一致：前置阶段（restrict/CAS/状态/AI）的未预期异常
        # 向上冒泡至全局错误处理器，仅 group/challenge/发送阶段的异常在
        # _process_user_join 内部捕获。锁始终由 async with 的 finally 释放。
        await _process_user_join(event, bot, chat_id, user_id, username)


async def _handle_approved_user_join(
    event: ChatMemberUpdated,
    bot: Bot,
    chat_id: int,
    user_id: int,
    approved_key: str,
) -> None:
    """处理已通过加入请求验证的用户正式入群：恢复权限并欢迎。"""
    user = event.new_chat_member.user
    logger.info(f"用户 {user_id} 已通过加入请求验证，跳过重复验证")

    chat_title = escape_html(event.chat.title) if event.chat.title else "群组"

    # ✅ 恢复用户权限（内部含重试，失败时降级通知）
    if not await restore_user_permissions(bot, chat_id, user_id):
        # 恢复失败：保留 approved_key，以便用户重新入群时再次尝试
        with contextlib.suppress(Exception):
            await send_verification_success_message(bot, user_id, chat_title, "restore_failed")
        return

    # 恢复成功：清除验证标记
    await get_redis().delete(approved_key)

    # 在私聊中通知用户
    with contextlib.suppress(Exception):
        await send_verification_success_message(bot, user_id, chat_title, "success")

    # 在群内发送欢迎消息
    welcome_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"✅ 欢迎 {format_user_mention(user)} 加入群组！",
    )

    # 5秒后删除欢迎消息
    await asyncio.sleep(5)
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)


async def _process_user_join(
    event: ChatMemberUpdated,
    bot: Bot,
    chat_id: int,
    user_id: int,
    username: str,
) -> None:
    """执行已取得 in-flight 锁的正式入群处理流程。"""
    user = event.new_chat_member.user
    redis = get_redis()
    verification_service = VerificationService()

    # ✅ 检查是否为管理员邀请（from_user 是邀请者）
    if event.from_user:
        inviter_id = event.from_user.id
        inviter_name = event.from_user.username or event.from_user.full_name

        # 检查邀请者是否是管理员
        is_admin_invite = await PermissionCache.is_admin(bot, chat_id, inviter_id)

        if is_admin_invite:
            logger.info(
                f"用户 {user_id} 由管理员 {inviter_name} ({inviter_id}) 邀请，" f"跳过验证直接通过"
            )

            # ✅ 清除可能存在的待验证状态（管理员批准加入请求场景）
            if await verification_service.is_verification_pending(chat_id, user_id):
                await verification_service.clear_verification(chat_id, user_id)
                logger.info(f"用户 {user_id} 由管理员邀请，已清除待验证状态")

            # 消费可能残留的 approved_key（管理员邀请/批准与 Bot 验证竞争时），使命已完成
            await redis.delete(RedisKeys.verification_approved(chat_id, user_id))

            # 直接发送欢迎消息（不需要限制权限）
            welcome_msg = await bot.send_message(
                chat_id=chat_id,
                text=f"✅ 欢迎 {format_user_mention(user)} 加入群组！\n\n"
                f"由管理员 {format_trusted_user_mention(event.from_user)} 邀请加入。",
            )

            # 5秒后删除欢迎消息
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

            return  # 管理员邀请，跳过后续验证流程

    try:
        # ⚠️ 立即限制新用户权限（防止在检测/验证期间发送垃圾消息）
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
                can_manage_topics=False,
            ),
        )
        logger.info(f"已限制用户 {user_id} 的所有权限")

    except Exception as e:
        logger.error(f"限制用户权限失败: {e}")
        # 权限限制失败，可能是 Bot 没有管理权限，记录日志但继续流程

    # 快速路径（restrict 之后）：已有进行中的验证则保持受限并返回，避免再次执行
    # CAS/状态/AI 检测。放在 restrict 之后是为了确保待验证用户保持无发言权限。
    if await verification_service.is_verification_pending(chat_id, user_id):
        logger.debug(
            f"用户 {user_id} 在群组 {chat_id} 已有进行中的验证（直接加入模式），忽略重复请求"
        )
        return

    # 已通过加入请求验证则恢复权限，避免再次执行 CAS/状态/AI 检测
    approved_key = RedisKeys.verification_approved(chat_id, user_id)
    if await redis.get(approved_key):
        await _handle_approved_user_join(event, bot, chat_id, user_id, approved_key)
        return

    # ========== CAS 黑名单检查 ==========
    if settings.cas_enabled:
        cas_service = get_cas_service()
        cas_result = await cas_service.check_user(user_id)

        if cas_result.is_banned:
            # 直接封禁并踢出
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            except Exception as e:
                logger.warning(f"CAS 封禁用户失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

            # 记录审计日志
            with contextlib.suppress(Exception):
                await AuditRepository.log_action(
                    group_id=chat_id,
                    operator_id=bot.id,
                    action="cas_ban_on_join",
                    target_user_id=user_id,
                    details={"offenses": cas_result.offenses},
                )

            # 发送群内通知（30 秒后自动删除）
            try:
                notify_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=f"🚫 {format_user_mention(user)} 在 CAS 黑名单中，已被自动封禁。",
                )
                await auto_delete_message(notify_msg, delay=30)
            except Exception as e:
                logger.warning(f"发送 CAS 封禁通知失败: {e}")

            logger.info(f"CAS 黑名单用户加入被拒 [群组:{chat_id}] [用户:{user_id}]")
            return  # 结束处理，不继续后续流程
    # ========== CAS 检查结束 ==========

    # ========== 用户状态检查（Telethon）==========
    if settings.user_status_check_enabled:
        status_service = get_user_status_service()
        status_result = await status_service.check_user(user_id, chat_id)

        if status_result.is_problematic:
            # 直接封禁并踢出
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            except Exception as e:
                logger.warning(f"封禁异常用户失败 [群组:{chat_id}] [用户:{user_id}]: {e}")

            # 记录审计日志
            with contextlib.suppress(Exception):
                await AuditRepository.log_action(
                    group_id=chat_id,
                    operator_id=bot.id,
                    action=f"user_status_ban_on_join_{status_result.reason}",
                    target_user_id=user_id,
                    details={"status": status_result.reason},
                )

            # 发送群内通知（30 秒后自动删除）
            try:
                status_map = {
                    "restricted": "被 Telegram 限制",
                    "scam": "被标记为诈骗账号",
                    "fake": "被标记为虚假账号",
                    "deleted": "已删除账号",
                }
                reason = status_result.reason or "未知状态"
                status_text = status_map.get(reason, reason)
                notify_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=f"🚫 {format_user_mention(user)} {status_text}，已被自动封禁。",
                )
                await auto_delete_message(notify_msg, delay=30)
            except Exception as e:
                logger.warning(f"发送封禁通知失败: {e}")

            logger.info(
                f"异常用户加入被拒 [群组:{chat_id}] [用户:{user_id}] "
                f"[状态:{status_result.reason}]"
            )
            return  # 结束处理，不继续后续流程
    # ========== 用户状态检查结束 ==========

    # ==================== 用户信息反垃圾检测 ====================
    if await check_user_spam_info(bot, chat_id, user_id, username, mode="join"):
        return  # 检测到垃圾信息，已处理，直接返回
    # ==================== 用户信息反垃圾检测结束 ====================

    try:
        # 用户未通过加入请求验证（未启用 Approve New Members），执行正常验证流程
        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id, event.chat.title)

        # 根据验证类型生成挑战
        challenge = await generate_verification_challenge(group, chat_id, user_id)

        # ✅ 尝试私聊发送验证消息
        try:
            # 发送验证消息
            sent_message = await send_verification_message(
                bot,
                chat_id,
                user_id,
                challenge,
                flow="join",
                username=username,
                timeout=group.verification_timeout,
            )

            logger.info(f"已向用户 {user_id} 私聊发送验证消息")

            # 启动超时处理任务
            asyncio.create_task(
                handle_verification_timeout(
                    bot, chat_id, user_id, sent_message.message_id, group.verification_timeout
                )
            )

        except TelegramForbiddenError:
            # ✅ 用户未启动 Bot，使用共享引导消息机制
            logger.warning(f"用户 {user_id} 未启动 Bot，无法发送私聊验证")
            await handle_user_not_started_bot(bot, chat_id, user_id, group.verification_timeout)

        except Exception as e:
            logger.error(f"发送私聊验证消息失败: {e}")
            # 踢出并封禁 1 小时
            await bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                until_date=datetime.now() + timedelta(hours=1),
            )

    except Exception as e:
        logger.error(f"处理用户加入事件失败: {e}")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_user_leave(event: ChatMemberUpdated) -> None:
    """处理用户离开群组事件 - 更新 username 映射

    用户离开群组时也会更新 username 映射，确保：
    1. 用户在离开前如果改了 username，映射会更新
    2. 保持映射是最新的状态
    3. 查询时通过 API 实时验证，离开群组的用户映射仍可正常工作
    """
    user = event.old_chat_member.user
    user_id = user.id
    username = user.username or user.full_name

    logger.info(f"用户 {username} ({user_id}) 离开群组 {event.chat.id}")

    # ✅ 更新 username 映射
    if user.username:
        await UsernameMappingService.update_mapping(
            user_id=user_id,
            username=user.username,
        )


async def _answer_default_toast(callback: CallbackQuery, key: str) -> None:
    """callback_data 无效（无法定位来源群）时用默认语言弹窗"""
    localizer = get_translator().for_locale(get_resolver().default_locale)
    await callback.answer(localizer.t(key), show_alert=True)


# 各验证类型答错时的 toast key
# math/qa/honeypot 是答案错误，slider/emoji/puzzle 是选择错误
_CHOICE_WRONG_KEYS: dict[str, str] = {
    "verify_math": "verification.callback.wrong_answer.toast",
    "verify_qa": "verification.callback.wrong_answer.toast",
    "verify_honeypot": "verification.callback.wrong_answer.toast",
    "verify_slider": "verification.callback.wrong_selection.toast",
    "verify_emoji": "verification.callback.wrong_selection.toast",
    "verify_puzzle": "verification.callback.wrong_selection.toast",
}


@router.callback_query(F.data.regexp(r"^verify_(?:math|slider|qa|emoji|honeypot|puzzle):"))
async def on_choice_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理选项类验证（math/slider/qa/emoji/honeypot/puzzle）- 私聊模式

    六类验证的 callback 协议一致（verify_<type>:chat_id:user_id:answer），逻辑统一：
    本人校验 → pending → verify_answer → 成功/失败处理。类型差异仅在答错 toast
    （_CHOICE_WRONG_KEYS）与 honeypot 陷阱。
    """
    if not callback.data or not callback.message:
        await _answer_default_toast(callback, "verification.callback.invalid_data.toast")
        return

    # 解析 callback_data（chat_id 仅作来源群候选，后续 is_verification_pending 隐式校验）
    try:
        parts = callback.data.split(":")
        if len(parts) != 4:
            raise ValueError("验证 callback 字段错误")
        prefix, chat_id_str, user_id_str, answer = parts
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
    except (TypeError, ValueError):
        await _answer_default_toast(callback, "verification.callback.invalid_data.toast")
        return

    # 私聊 callback 展示语言按「用户偏好 → 来源群 locale」解析（选项 B）
    private_locale = await get_resolver().for_private_from_group(
        user_id=user_id, group_chat_id=chat_id
    )
    localizer = get_translator().for_locale(private_locale)

    try:
        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer(
                localizer.t("verification.callback.not_yours.toast"), show_alert=True
            )
            return

        # ✅ 检查验证状态是否还存在（已通过 / 已超时被踢 / 点击过期消息）
        verification_service = VerificationService()
        if not await verification_service.is_verification_pending(chat_id, user_id):
            await callback.answer(
                localizer.t("verification.callback.expired.toast"), show_alert=False
            )
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            return

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, answer):
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            is_join_request = (await redis.get(type_key)) == "join_request"
            await redis.delete(type_key)
            await handle_verification_success(bot, callback, chat_id, user_id, is_join_request)
        else:
            # 类型差异化答错 toast（honeypot 陷阱单独处理）
            if prefix == "verify_honeypot" and answer == "trap":
                await callback.answer(
                    localizer.t("verification.callback.honeypot_trap.toast"), show_alert=True
                )
                logger.warning(f"用户 {user_id} 触发蜜罐陷阱")
            else:
                await callback.answer(localizer.t(_CHOICE_WRONG_KEYS[prefix]), show_alert=True)

            # 根据验证类型决定踢出或拒绝
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)

            if verification_type == "join_request":
                # 拒绝加入请求并封禁1小时，防止立即重试
                await decline_join_request(bot, chat_id, user_id)
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=datetime.now() + timedelta(hours=1),
                )
                await redis.delete(type_key)
            else:
                # 踢出并封禁 1 小时
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=datetime.now() + timedelta(hours=1),
                )

            # ✅ 清除验证状态，避免 timeout 任务重复处理（修复 HIDE_REQUESTER_MISSING）
            await verification_service.clear_verification(chat_id, user_id)

            # 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            logger.info(f"用户 {user_id} 验证失败")

    except Exception as e:
        logger.error(f"处理选项验证失败: {e}")
        await callback.answer(localizer.t("verification.callback.failed.toast"), show_alert=True)


@router.callback_query(F.data.startswith("verify_captcha_input:"))
async def on_captcha_input_request(callback: CallbackQuery) -> None:
    """处理验证码输入请求 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 获取群组配置的超时时间
        group_repo = GroupRepository()
        group_config = await group_repo.get(chat_id)
        timeout = group_config.verification_timeout if group_config else 120

        # 设置等待输入状态（TTL 稍长一点留缓冲）
        redis = get_redis()
        waiting_key = RedisKeys.captcha_waiting(chat_id, user_id)
        await redis.setex(waiting_key, timeout + 10, str(callback.message.message_id))

        # ✅ 安全修复：设置反向索引，避免 Redis SCAN DoS 攻击
        waiting_user_key = RedisKeys.captcha_waiting_user(user_id)
        await redis.setex(waiting_user_key, timeout + 10, str(chat_id))

        await callback.answer("✏️ 请直接发送验证码文本", show_alert=False)

    except Exception as e:
        logger.error(f"处理验证码输入请求失败: {e}")
        await callback.answer("❌ 操作失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_captcha_refresh:"))
async def on_captcha_refresh(callback: CallbackQuery, bot: Bot) -> None:
    """处理验证码刷新 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 重新生成验证码
        verification_service = VerificationService()
        username = callback.from_user.full_name

        # 获取群组配置获取超时时间
        group_repo = GroupRepository()
        group_config = await group_repo.get(chat_id)
        timeout = group_config.verification_timeout if group_config else 120

        challenge = await verification_service.generate_captcha_challenge(chat_id, user_id, timeout)

        # 刷新后仅更新题面与按钮（保持原行为：不重复信封标题），按 locale 渲染
        locale = await get_resolver().for_private_from_group(user_id=user_id, group_chat_id=chat_id)
        localizer = get_translator().for_locale(locale)
        rendered = render_captcha_for_refresh(
            challenge, localizer, chat_id, user_id, username, timeout
        )

        # 检查 photo 是否存在
        if rendered.photo is None:
            await callback.answer("❌ 生成验证码失败", show_alert=True)
            return

        # 编辑消息，更新图片和按钮
        await bot.edit_message_media(
            chat_id=user_id,
            message_id=callback.message.message_id,
            media=InputMediaPhoto(media=rendered.photo),
        )
        await bot.edit_message_caption(
            chat_id=user_id,
            message_id=callback.message.message_id,
            caption=rendered.text,
            reply_markup=(
                rendered.keyboard if isinstance(rendered.keyboard, InlineKeyboardMarkup) else None
            ),
        )
        await callback.answer("🔄 已刷新验证码", show_alert=False)

    except Exception as e:
        logger.error(f"处理验证码刷新失败: {e}")
        await callback.answer("❌ 刷新失败，请联系管理员", show_alert=True)


@router.message(F.chat.type == "private", F.text, ~F.web_app_data)
async def on_captcha_text_input(message: Message, bot: Bot) -> None:
    """处理验证码文本输入 - 私聊消息

    注意：明确排除 web_app_data 消息，避免拦截 CAPTCHA WebApp 回调
    """
    # 类型检查
    if not message.from_user or not message.text:
        return

    # 调试日志
    logger.debug(
        f"[CAPTCHA] 收到私聊文本 [user:{message.from_user.id}] "
        f"[has_webapp:{message.web_app_data is not None}]"
    )

    try:
        user_id = message.from_user.id
        text_input = message.text.strip()

        # 检查是否在等待验证码输入
        redis = get_redis()

        # ✅ 安全修复：使用反向索引直接获取 chat_id，避免 Redis SCAN DoS 攻击
        waiting_user_key = RedisKeys.captcha_waiting_user(user_id)
        chat_id_str = await redis.get(waiting_user_key)

        if not chat_id_str:
            # 没有待验证状态，忽略消息
            return

        chat_id = int(chat_id_str)

        # ✅ 检查验证状态是否存在
        verification_service = VerificationService()
        if not await verification_service.is_verification_pending(chat_id, user_id):
            # 验证状态不存在，清理 waiting 状态
            await redis.delete(waiting_user_key)
            waiting_key = RedisKeys.captcha_waiting(chat_id, user_id)
            await redis.delete(waiting_key)
            return

        # 检查验证状态类型
        verification_key = RedisKeys.verification(chat_id, user_id)
        stored_value = await redis.get(verification_key)
        if not stored_value or not stored_value.startswith("captcha:"):
            # 不是 captcha 验证或已过期
            return

        # 检查是否在等待输入状态
        waiting_key = RedisKeys.captcha_waiting(chat_id, user_id)
        message_id_str = await redis.get(waiting_key)

        if not message_id_str:
            # 未点击"输入验证码"按钮
            return

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, text_input):
            # 验证成功 - 清理所有相关键
            await redis.delete(waiting_key)
            await redis.delete(waiting_user_key)  # ✅ 删除反向索引

            # 检查验证类型
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            # 删除验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=int(message_id_str))

            # ✅ 清除验证状态（提前到可能早退之前，避免遗留状态被超时任务重复处理）
            await verification_service.clear_verification(chat_id, user_id)

            await message.answer("✅ 验证成功！")

            # ✅ 设置"已验证"标记（10 分钟），以便权限恢复/批准失败后用户重新触发入群时跳过验证
            approved_key = RedisKeys.verification_approved(chat_id, user_id)
            await redis.setex(approved_key, 600, "1")

            # 获取群组标题（用于失败降级文案）
            chat_title = "群组"
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"

            # 处理验证成功逻辑
            if is_join_request:
                # 批准加入请求（内部含重试）
                if not await approve_join_request(bot, chat_id, user_id):
                    with contextlib.suppress(Exception):
                        await send_verification_success_message(
                            bot, user_id, chat_title, "approve_failed"
                        )
                    return
                logger.info(f"用户 {user_id} 验证成功，已批准加入请求")
            else:
                # ✅ 恢复用户权限（内部含重试，失败时降级通知）
                if not await restore_user_permissions(bot, chat_id, user_id):
                    with contextlib.suppress(Exception):
                        await send_verification_success_message(
                            bot, user_id, chat_title, "restore_failed"
                        )
                    return

                # 恢复成功：approved_key 使命完成，删除避免 10 分钟内重新加入免验证
                await redis.delete(approved_key)

                # 发送欢迎消息到群组
                assert message.from_user  # 类型缩小
                welcome_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ 欢迎 {masked_mention_html(message.from_user)} 加入群组！",
                    parse_mode="HTML",
                )

                # 30 秒后删除欢迎消息
                async def delayed_delete():
                    await asyncio.sleep(30)
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

                asyncio.create_task(delayed_delete())

                logger.info(f"用户 {user_id} 验证成功")

        else:
            # 验证失败 - 清理所有相关键
            await message.answer("❌ 验证码错误，请重试")
            await redis.delete(waiting_key)
            await redis.delete(waiting_user_key)  # ✅ 删除反向索引

            # 根据验证类型决定踢出或拒绝
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)

            if verification_type == "join_request":
                # 拒绝加入请求
                await decline_join_request(bot, chat_id, user_id)
                await redis.delete(type_key)
            else:
                # 踢出并封禁 1 小时
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=datetime.now() + timedelta(hours=1),
                )

                # 删除验证消息
                with contextlib.suppress(Exception):
                    await bot.delete_message(chat_id=user_id, message_id=int(message_id_str))

            # ✅ 清除验证状态，避免 timeout 任务重复处理（修复 HIDE_REQUESTER_MISSING）
            verification_service = VerificationService()
            await verification_service.clear_verification(chat_id, user_id)

            logger.info(f"用户 {user_id} 验证码验证失败")

    except Exception as e:
        logger.error(f"处理验证码文本输入失败: {e}")


@router.callback_query(F.data.startswith("verify_cancel:"))
async def on_verify_cancel(callback: CallbackQuery, bot: Bot) -> None:
    """处理取消验证 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # ✅ 检查验证状态是否还存在
        verification_service = VerificationService()
        if not await verification_service.is_verification_pending(chat_id, user_id):
            await callback.answer("✅ 此验证消息已失效", show_alert=False)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            return

        # 踢出并封禁 1 小时
        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=datetime.now() + timedelta(hours=1),
        )

        # 删除私聊中的验证消息
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)

        # 清除验证状态
        await verification_service.clear_verification(chat_id, user_id)

        await callback.answer("已取消验证")
        logger.info(f"用户 {user_id} 取消验证，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理取消验证失败: {e}")


@router.message(F.web_app_data)
async def on_webapp_data(message: Message, bot: Bot) -> None:
    """处理 WebApp 返回的数据（所有 CAPTCHA 验证回调）"""
    import hashlib
    import hmac
    import json
    import time

    from src.core.config import settings

    # 类型检查
    if not message.from_user or not message.web_app_data:
        return

    # 调试：记录收到 WebApp 数据
    logger.info(
        f"✅ 收到 WebApp 数据 [from_user:{message.from_user.id}] "
        f"[data_length:{len(message.web_app_data.data)}]"
    )
    logger.debug(f"WebApp 原始数据: {message.web_app_data.data}")

    try:
        data = json.loads(message.web_app_data.data)
        logger.debug(f"WebApp 解析后数据: {data}")

        # 支持两种 action 格式：
        # 1. 推荐格式：captcha_success + provider 字段
        # 2. 兼容格式：{provider}_success（如 turnstile_success）
        action = data.get("action", "")
        if action == "captcha_success":
            provider = data.get("provider", "")
            if not provider:
                logger.warning("WebApp 数据缺少 provider 字段")
                return
        elif action.endswith("_success"):
            # 兼容旧格式：turnstile_success -> turnstile
            provider = action.replace("_success", "")
        else:
            logger.warning(f"WebApp 数据 action 不匹配: {action}")
            return

        chat_id = int(data["chat_id"])
        user_id = int(data["user_id"])
        verify_token = data["verify_token"]
        signature = data["signature"]
        timestamp = int(data["timestamp"])

        logger.info(f"收到 {provider.upper()} WebApp 回调 [user:{user_id}] [chat:{chat_id}]")

        # 1. 验证时间戳（防止重放，5 分钟内有效）
        if abs(time.time() - timestamp) > 300:
            logger.warning(f"{provider.upper()} 回调时间戳过期 [user:{user_id}]")
            # 通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text="❌ <b>验证失败</b>\n\n验证请求已过期，请重新尝试。",
                    parse_mode="HTML",
                )
            return

        # 2. 验证签名（使用统一 CAPTCHA 签名密钥）
        signature_key = settings.captcha_signature_key
        if not signature_key:
            logger.error(
                f"未配置 CAPTCHA_SIGNATURE_KEY [provider:{provider}]\n"
                f"请在 .env 文件中设置 CAPTCHA_SIGNATURE_KEY"
            )
            return

        expected_sig = hmac.new(
            signature_key.encode(),
            f"{chat_id}:{user_id}:{verify_token}:{timestamp}".encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            logger.warning(
                f"{provider.upper()} 回调签名无效 [user:{user_id}] [expected:{expected_sig[:8]}...] [got:{signature[:8]}...]"
            )
            # 通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text="❌ <b>验证失败</b>\n\n签名验证失败，请联系管理员检查配置。",
                    parse_mode="HTML",
                )
            return

        # 3. 验证 Redis 中的 token 存在（一次性）
        redis = get_redis()

        # 统一使用 captcha_token 键
        token_key = RedisKeys.captcha_token(chat_id, user_id)
        stored_token_data = await redis.get(token_key)

        # 检查 token
        if not stored_token_data:
            logger.warning(
                f"CAPTCHA 验证 token 不存在或已过期 [provider:{provider}] [user:{user_id}]"
            )
            # 通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text="❌ <b>验证失败</b>\n\n验证令牌无效或已使用，请重新尝试。",
                    parse_mode="HTML",
                )
            return

        # 解析 token（统一格式：provider:token[:key_index]）
        token_parts = stored_token_data.split(":")
        if len(token_parts) < 2 or token_parts[0] != provider:
            logger.warning(
                f"CAPTCHA 验证 token 格式错误 [provider:{provider}] [user:{user_id}] [stored:{stored_token_data}]"
            )
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text="❌ <b>验证失败</b>\n\n验证令牌格式错误，请重新尝试。",
                    parse_mode="HTML",
                )
            return

        stored_token = token_parts[1]

        if stored_token != verify_token:
            logger.warning(
                f"{provider.upper()} token 不匹配 [user:{user_id}] [stored:{stored_token[:8]}...] [got:{verify_token[:8]}...]"
            )
            # 通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text="❌ <b>验证失败</b>\n\n验证令牌无效，请重新尝试。",
                    parse_mode="HTML",
                )
            return

        # 4. 删除 token（防止重复使用）
        await redis.delete(token_key)

        # 5. 检查验证类型
        type_key = RedisKeys.verification_type(chat_id, user_id)
        verification_type = await redis.get(type_key)
        is_join_request = verification_type == "join_request"

        # 清除类型标记
        await redis.delete(type_key)

        # ✅ 6. 立即清除验证状态（防止超时任务踢人）
        # 无论后续操作是否成功，验证已经通过，不应该再触发超时
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id)
        logger.info(f"已清除用户 {user_id} 的验证状态 (群组 {chat_id})")

        # 7. 验证成功，恢复权限
        if is_join_request:
            # 加入请求模式：批准加入请求
            approved_key = RedisKeys.verification_approved(chat_id, user_id)
            await redis.setex(approved_key, 600, "1")  # 10分钟

            approved = await approve_join_request(bot, chat_id, user_id)
            if approved:
                logger.info(
                    f"用户 {user_id} {provider.upper()} 验证成功，已批准加入请求 (群组 {chat_id})"
                )
            else:
                # 批准失败（重试耗尽）：approved_key 保留，用户重新提交加入请求时可自动批准
                logger.warning(
                    f"用户 {user_id} {provider.upper()} 批准加入请求失败（重试耗尽），"
                    f"approved_key 保留以便重新提交加入请求"
                )

            # 在私聊中通知用户
            message_type = "success_join_request" if approved else "approve_failed"
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"
                await send_verification_success_message(bot, user_id, chat_title, message_type)

        else:
            # 正常入群模式：恢复群组权限

            # ✅ 设置"已验证"标记（10分钟有效），防止恢复权限失败后用户重新加入被要求验证
            approved_key = RedisKeys.verification_approved(chat_id, user_id)
            await redis.setex(approved_key, 600, "1")  # 10分钟

            # ✅ 恢复用户权限（修复 bug：不再踢出用户）
            success = await restore_user_permissions(bot, chat_id, user_id)

            if not success:
                # 恢复权限失败，通知用户稍后重试或联系管理员
                with contextlib.suppress(Exception):
                    chat = await bot.get_chat(chat_id)
                    chat_title = escape_html(chat.title) if chat.title else "群组"
                    await send_verification_success_message(
                        bot, user_id, chat_title, "restore_failed"
                    )
                return  # 提前返回，不发送欢迎消息

            # 恢复成功：approved_key 使命完成，删除避免 10 分钟内重新加入免验证
            await redis.delete(approved_key)

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"
                await send_verification_success_message(bot, user_id, chat_title, "success")

            # 在群内发送欢迎消息
            user = message.from_user
            welcome_msg = await bot.send_message(
                chat_id=chat_id,
                text=f"✅ 欢迎 {format_user_mention(user)} 加入群组！",
            )

            # 5秒后删除欢迎消息
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

            logger.info(f"用户 {user_id} {provider.upper()} 验证成功 (群组 {chat_id})")

    except Exception as e:
        logger.error(f"处理 CAPTCHA 回调失败: {e}")

        # ✅ 异常时也要清除验证状态，避免超时任务踢人
        try:
            # 尝试从异常上下文中获取 chat_id 和 user_id
            if "data" in locals() and "chat_id" in data and "user_id" in data:
                chat_id = int(data["chat_id"])
                user_id = int(data["user_id"])
                verification_service = VerificationService()
                await verification_service.clear_verification(chat_id, user_id)
                logger.info(f"异常处理：已清除用户 {user_id} 的验证状态 (群组 {chat_id})")

                # 通知用户验证失败
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        chat_id=user_id,
                        text="❌ <b>验证失败</b>\n\n处理验证时发生错误，请重新尝试。如果问题持续，请联系管理员。",
                        parse_mode="HTML",
                    )
        except Exception as cleanup_error:
            logger.error(f"清除验证状态失败: {cleanup_error}")


async def handle_verification_success(
    bot: Bot, callback: CallbackQuery, chat_id: int, user_id: int, is_join_request: bool = False
) -> None:
    """处理验证成功

    Args:
        is_join_request: 是否为加入请求验证（True: 批准请求, False: 恢复权限）

    所有验证类型走 i18n（私聊按「用户偏好 → 来源群 locale」、群欢迎按群 locale）。
    """
    private_locale = await get_resolver().for_private_from_group(
        user_id=user_id, group_chat_id=chat_id
    )
    private_localizer = get_translator().for_locale(private_locale)

    def _toast(key: str) -> str:
        return private_localizer.t(key)

    try:
        # 获取群组信息
        chat = await bot.get_chat(chat_id)
        chat_title = escape_html(chat.title) if chat.title else "群组"  # ✅ 安全修复：转义 HTML

        # 清除验证状态
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id)

        # 删除私聊中的验证消息
        with contextlib.suppress(Exception):
            if callback.message:
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)

        if is_join_request:
            # 加入请求模式：批准加入请求
            redis = get_redis()
            approved_key = RedisKeys.verification_approved(chat_id, user_id)

            # 标记用户已验证（10分钟有效期）
            await redis.setex(approved_key, 600, "1")  # 10分钟

            # 批准加入请求（内部含重试）
            if not await approve_join_request(bot, chat_id, user_id):
                with contextlib.suppress(Exception):
                    await send_verification_success_message(
                        bot,
                        user_id,
                        chat_title,
                        "approve_failed",
                        group_chat_id=chat_id,
                    )
                await callback.answer(
                    _toast("verification.callback.approve_failed.toast"),
                    show_alert=True,
                )
                return

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await send_verification_success_message(
                    bot,
                    user_id,
                    chat_title,
                    "success_join_request",
                    group_chat_id=chat_id,
                )

            await callback.answer(_toast("verification.callback.success.toast"))
            logger.info(f"用户 {user_id} 加入请求验证成功，已批准加入群组 {chat_id}")

        else:
            # 正常入群模式：恢复群组权限
            redis = get_redis()
            approved_key = RedisKeys.verification_approved(chat_id, user_id)
            # ✅ 设置"已验证"标记（10 分钟），以便权限恢复失败后用户重新加入时跳过验证
            await redis.setex(approved_key, 600, "1")

            # ✅ 恢复用户权限（内部含重试，失败时降级通知）
            if not await restore_user_permissions(bot, chat_id, user_id):
                with contextlib.suppress(Exception):
                    await send_verification_success_message(
                        bot,
                        user_id,
                        chat_title,
                        "restore_failed",
                        group_chat_id=chat_id,
                    )
                await callback.answer(
                    _toast("verification.callback.restore_failed.toast"),
                    show_alert=True,
                )
                return

            # 恢复成功：approved_key 使命完成，删除避免 10 分钟内重新加入免验证
            await redis.delete(approved_key)

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await send_verification_success_message(
                    bot,
                    user_id,
                    chat_title,
                    "success",
                    group_chat_id=chat_id,
                )

            # ✅ 先结束 Telegram callback spinner，再发群欢迎并等待删除（避免 spinner 拖慢 5 秒）
            await callback.answer(_toast("verification.callback.success.toast"))

            # 在群内发送欢迎消息（仅此一条群内消息，按群 locale）
            group_locale = await get_resolver().for_group(chat_id)
            welcome_text = (
                get_translator()
                .for_locale(group_locale)
                .t("verification.join.group.welcome", user=format_user_mention(callback.from_user))
            )
            welcome_msg = await bot.send_message(chat_id=chat_id, text=welcome_text)

            # 5秒后删除欢迎消息
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

            logger.info(f"用户 {user_id} 私聊验证成功，已加入群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理验证成功失败: {e}")
        with contextlib.suppress(Exception):
            await callback.answer(
                _toast("verification.callback.failed.toast"),
                show_alert=True,
            )


async def handle_verification_timeout(
    bot: Bot, chat_id: int, user_id: int, message_id: int, timeout: int
) -> None:
    """处理验证超时 - 私聊验证模式"""
    try:
        # 等待超时时间
        await asyncio.sleep(timeout)

        # 检查验证状态
        verification_service = VerificationService()
        if await verification_service.is_verification_pending(chat_id, user_id):
            # 验证超时
            logger.info(f"用户 {user_id} 验证超时（{timeout}秒），开始处理...")

            # 1. 踢出并封禁 1 小时
            try:
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=datetime.now() + timedelta(hours=1),
                )
                logger.info(f"已踢出并封禁用户 {user_id} 1小时（验证超时）")
            except Exception as e:
                logger.error(f"踢出用户失败: {e}")

            # 2. ✅ 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=message_id)

            # 3. ✅ 在私聊中通知用户（可选）
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = (
                    escape_html(chat.title) if chat.title else "群组"
                )  # ✅ 安全修复：转义 HTML
                timeout_locale = await get_resolver().for_private_from_group(
                    user_id=user_id, group_chat_id=chat_id
                )
                timeout_text = (
                    get_translator()
                    .for_locale(timeout_locale)
                    .t(
                        "verification.timeout.private.join.message",
                        chat_title=chat_title,
                        timeout=timeout,
                    )
                )
                await bot.send_message(
                    chat_id=user_id,
                    text=timeout_text,
                    parse_mode="HTML",  # ✅ 安全修复：使用 HTML 代替 Markdown
                )

            # 4. ✅ 群内不发送任何消息

            # 5. 清除验证状态
            await verification_service.clear_verification(chat_id, user_id)

            logger.info(f"用户 {user_id} 验证超时处理完成（已踢出+封禁1小时）")
        else:
            logger.debug(f"用户 {user_id} 验证状态已清除（可能已完成验证或被清除）")

    except Exception as e:
        logger.error(f"处理验证超时失败: {e}")


async def handle_user_not_started_bot(bot: Bot, chat_id: int, user_id: int, timeout: int) -> None:
    """
    处理用户未启动 Bot 的情况 - 共享引导消息机制 + 延迟踢出

    优化: 30秒内只发送一条群内引导消息，多用户共享
    策略: 不立即踢出，给用户完整的验证超时时间去启动 bot 和完成验证
    """
    redis = get_redis()
    hint_key = RedisKeys.verification_hint(chat_id)

    # 1. 检查是否已经发送过引导消息（30秒内）
    existing_hint = await redis.get(hint_key)

    if not existing_hint:
        # 2. 如果没有，发送通用引导消息
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username

            # 获取群组信息
            try:
                chat = await bot.get_chat(chat_id)
                chat_title = (
                    escape_html(chat.title) if chat.title else "本群组"
                )  # ✅ 安全修复：转义 HTML
            except Exception:
                chat_title = "本群组"

            # 创建启动链接按钮
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🤖 启动 Bot 进行验证",
                            url=f"https://t.me/{bot_username}?start=verify_{chat_id}",
                        )
                    ]
                ]
            )

            # 发送通用引导消息
            hint_msg = await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ <b>入群验证提示</b>\n\n"
                    f"欢迎加入 <b>{chat_title}</b>！\n\n"  # ✅ 安全修复：转义 HTML
                    "📱 本群使用 Bot 私聊验证，如果您刚加入但未收到验证消息，"
                    "说明您尚未启动 Bot。\n\n"
                    "👉 请点击下方按钮启动 Bot 进行验证：\n\n"
                    "✅ 启动后会立即收到验证消息\n"
                    "💡 此提示将在 30 秒后自动删除"
                ),
                reply_markup=keyboard,
                parse_mode="HTML",  # ✅ 安全修复：使用 HTML 代替 Markdown
            )

            # 3. 记录到 Redis（30秒过期）
            await redis.setex(hint_key, 30, f"message_id:{hint_msg.message_id}")

            # 4. 启动自动删除任务
            asyncio.create_task(
                delete_hint_message_after_delay(bot, chat_id, hint_msg.message_id, hint_key, 30)
            )

            logger.info(f"群组 {chat_id} 发送入群验证引导消息（30秒内共享）")

        except Exception as e:
            logger.error(f"发送引导消息失败: {e}")
    else:
        # ✅ 已有引导消息，延长 TTL 到 30 秒，让后入群用户有足够时间
        await redis.expire(hint_key, 30)
        logger.debug(f"群组 {chat_id} 已有引导消息，延长 TTL 到 30 秒（用户 {user_id}）")

    # 5. 启动延迟踢出任务（延迟时间 = 验证超时时间）
    # 如果用户在超时前启动 bot 并完成验证，验证成功流程会清除验证状态，导致踢出任务检查失败
    asyncio.create_task(
        handle_verification_timeout(bot, chat_id, user_id, message_id=0, timeout=timeout)
    )
    logger.info(
        f"用户 {user_id} 未启动 Bot，已保留在群组 {chat_id} 但限制权限，{timeout} 秒后将踢出"
    )


async def delete_hint_message_after_delay(
    bot: Bot, chat_id: int, message_id: int, hint_key: str, delay: int
):
    """延迟删除引导消息

    支持 TTL 延长：如果在等待期间 Redis key 被延长，会继续等待剩余时间
    """
    try:
        await asyncio.sleep(delay)

        # ✅ 检查 Redis key 是否还存在（可能被延长了）
        redis = get_redis()
        remaining_ttl = await redis.ttl(hint_key)

        if remaining_ttl > 0:
            # Key 还存在且被延长了，继续等待剩余时间
            logger.debug(f"群组 {chat_id} 的引导消息 TTL 被延长，继续等待 {remaining_ttl} 秒")
            asyncio.create_task(
                delete_hint_message_after_delay(bot, chat_id, message_id, hint_key, remaining_ttl)
            )
            return

        # TTL 已过期或 key 不存在，删除消息
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"已删除群组 {chat_id} 的引导消息 {message_id}")
    except Exception as e:
        logger.debug(f"删除引导消息失败（可能已被手动删除）: {e}")


async def handle_join_request_timeout(
    bot: Bot, chat_id: int, user_id: int, message_id: int, timeout: int
) -> None:
    """处理加入请求验证超时 - 拒绝加入请求"""
    try:
        # 等待超时时间
        await asyncio.sleep(timeout)

        # 检查验证状态
        verification_service = VerificationService()
        if await verification_service.is_verification_pending(chat_id, user_id):
            # 验证超时
            logger.info(f"用户 {user_id} 加入请求验证超时（{timeout}秒），开始处理...")

            # 1. 拒绝加入请求
            try:
                await decline_join_request(bot, chat_id, user_id)
                logger.info(f"已拒绝用户 {user_id} 的加入请求")
            except Exception as e:
                logger.error(f"拒绝加入请求失败: {e}")

            # 2. 封禁 1 小时，防止立即重试
            try:
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=datetime.now() + timedelta(hours=1),
                )
                logger.info(f"已封禁用户 {user_id} 1小时（验证超时）")
            except Exception as e:
                logger.error(f"封禁用户失败: {e}")

            # 3. 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=message_id)

            # 4. 在私聊中通知用户
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = (
                    escape_html(chat.title) if chat.title else "群组"
                )  # ✅ 安全修复：转义 HTML
                timeout_locale = await get_resolver().for_private_from_group(
                    user_id=user_id, group_chat_id=chat_id
                )
                timeout_text = (
                    get_translator()
                    .for_locale(timeout_locale)
                    .t(
                        "verification.timeout.private.join_request.message",
                        chat_title=chat_title,
                        timeout=timeout,
                    )
                )
                await bot.send_message(
                    chat_id=user_id,
                    text=timeout_text,
                    parse_mode="HTML",
                )

            # 5. 清除验证状态
            await verification_service.clear_verification(chat_id, user_id)

            logger.info(f"用户 {user_id} 加入请求验证超时处理完成（已拒绝+封禁1小时）")
        else:
            logger.debug(f"用户 {user_id} 验证状态已清除（可能已完成验证或被清除）")

    except Exception as e:
        logger.error(f"处理加入请求验证超时失败: {e}")


async def handle_user_not_started_bot_for_join_request(
    bot: Bot, chat_id: int, user_id: int
) -> None:
    """处理加入请求中用户未启动 Bot - 共享引导消息 + 拒绝加入请求"""
    redis = get_redis()
    hint_key = RedisKeys.verification_hint(chat_id)

    # 1. 检查是否已经发送过引导消息（30秒内）
    existing_hint = await redis.get(hint_key)

    if not existing_hint:
        # 2. 如果没有，发送通用引导消息
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username

            # 获取群组信息
            try:
                chat = await bot.get_chat(chat_id)
                chat_title = (
                    escape_html(chat.title) if chat.title else "本群组"
                )  # ✅ 安全修复：转义 HTML
            except Exception:
                chat_title = "本群组"

            # 创建启动链接按钮
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🤖 启动 Bot 进行验证",
                            url=f"https://t.me/{bot_username}?start=verify_{chat_id}",
                        )
                    ]
                ]
            )

            # 发送通用引导消息
            hint_msg = await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ <b>入群验证提示</b>\n\n"
                    f"欢迎请求加入 <b>{chat_title}</b>！\n\n"  # ✅ 安全修复：转义 HTML
                    "📱 本群使用 Bot 私聊验证，如果您发送了加入请求但未收到验证消息，"
                    "说明您尚未启动 Bot。\n\n"
                    "👉 请点击下方按钮启动 Bot 后重新发送加入请求：\n\n"
                    "✅ 启动后重新请求加入即可收到验证消息\n"
                    "💡 此提示将在 30 秒后自动删除"
                ),
                reply_markup=keyboard,
                parse_mode="HTML",  # ✅ 安全修复：使用 HTML 代替 Markdown
            )

            # 3. 记录到 Redis（30秒过期）
            await redis.setex(hint_key, 30, f"message_id:{hint_msg.message_id}")

            # 4. 启动自动删除任务
            asyncio.create_task(
                delete_hint_message_after_delay(bot, chat_id, hint_msg.message_id, hint_key, 30)
            )

            logger.info(f"群组 {chat_id} 发送加入请求验证引导消息（30秒内共享）")

        except Exception as e:
            logger.error(f"发送引导消息失败: {e}")
    else:
        # 已有引导消息，延长 TTL 到 30 秒
        await redis.expire(hint_key, 30)
        logger.debug(f"群组 {chat_id} 已有引导消息，延长 TTL 到 30 秒（用户 {user_id}）")

    # 5. 拒绝加入请求
    try:
        await decline_join_request(bot, chat_id, user_id)
        logger.info(f"用户 {user_id} 未启动 Bot，已拒绝加入请求（群组 {chat_id}）")
    except Exception as e:
        logger.error(f"拒绝加入请求失败: {e}")

    # 6. ✅ 清除验证状态，避免 timeout 任务重复处理（修复 HIDE_REQUESTER_MISSING）
    verification_service = VerificationService()
    await verification_service.clear_verification(chat_id, user_id)
