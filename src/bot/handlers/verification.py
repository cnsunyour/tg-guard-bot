"""入群验证处理器"""

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal

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
    User,
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
from src.services.verification import (
    CaptchaChallenge,
    PreparedChallenge,
    VerificationChallenge,
    VerificationService,
)
from src.services.verification_hint import (
    VerificationHintFlow,
    delete_hint_reservation,
    get_hint_ttl_if_match,
    promote_hint,
    reserve_hint,
    try_extend_hint,
)
from src.services.verification_recovery import (
    VerificationClearToken,
    claim_timeout,
    commit_recovery,
    new_revision_id,
    new_session_id,
    promote_recovery,
    release_recovery,
    reserve_initial_recovery,
    reserve_recovery,
)

router = Router(name="verification")

# 初始发送编排结果：sent 已发送 / undelivered 未启动 Bot（可恢复）/ busy 残留旧会话
type InitialDeliveryResult = Literal["sent", "undelivered", "busy"]

# 安全释放 in-flight 锁的 Lua 脚本：仅当键值等于 owner token 时才删除，
# 避免「单次处理耗时超过 TTL → 旧协程 finally 误删新协程刚取得的锁」。
_INFLIGHT_RELEASE_SCRIPT = (
    'if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) end return 0'
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


# 验证结果私聊消息的四种类型（catalog: verification.success.private.<type>.message）
type SuccessMessageType = Literal[
    "success", "success_join_request", "restore_failed", "approve_failed"
]


async def send_verification_success_message(
    bot: Bot,
    user_id: int,
    chat_title: str,
    message_type: SuccessMessageType,
    *,
    group_chat_id: int,
) -> None:
    """发送验证结果私聊消息（按用户偏好 → 来源群 locale 渲染）。

    所有关键操作（权限恢复、加入请求批准）均由调用方带重试执行，失败时传入
    对应的 ``*_failed`` 类型，由本函数发送降级引导文案。不再创建邀请链接：
    restricted 用户点击「加入群组」链接无效，重试 + 联系管理员更可靠。

    Args:
        bot: Bot 实例
        user_id: 用户 ID
        chat_title: 群组标题（已 escape HTML）
        message_type: 结果类型（success / success_join_request / restore_failed /
            approve_failed）
        group_chat_id: 来源群 ID（必传，用于解析私聊 locale 并保证文案走 catalog）。
    """
    locale = await get_resolver().for_private_from_group(
        user_id=user_id, group_chat_id=group_chat_id
    )
    localizer = get_translator().for_locale(locale)
    text = localizer.t(
        f"verification.success.private.{message_type}.message",
        chat_title=chat_title,
    )
    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


type WelcomeMentionStyle = Literal["plain", "linked"]


async def send_group_welcome(
    bot: Bot,
    chat_id: int,
    user: User,
    *,
    mention_style: WelcomeMentionStyle = "plain",
) -> Message:
    """在群内发送欢迎消息（按群 locale 渲染），返回消息供调用方延迟删除。

    mention_style 控制用户提及格式（均经脱敏，普通用户不暴露真实名称）：
    - plain: format_user_mention 纯文本（脱敏名 + @脱敏username 或数字 ID）
    - linked: masked_mention_html 可点击 <a> 链接（仅脱敏名，管理员可点击定位）
    """
    group_locale = await get_resolver().for_group(chat_id)
    mention = masked_mention_html(user) if mention_style == "linked" else format_user_mention(user)
    welcome_text = (
        get_translator().for_locale(group_locale).t("verification.join.group.welcome", user=mention)
    )
    return await bot.send_message(chat_id=chat_id, text=welcome_text, parse_mode="HTML")


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

            # 副作用前捕获快照（decline/ban 经网络，clear 须用旧 session 快照防删新 session）
            verification_service = VerificationService()
            clear_token = await verification_service.capture_clear_token(chat_id, user_id)

            # 处理加入请求模式：先拒绝加入请求
            if mode == "join_request":
                try:
                    await decline_join_request(bot, chat_id, user_id)
                    logger.info(f"已拒绝垃圾用户 {user_id} 的加入请求")
                except Exception as decline_error:
                    logger.error(f"拒绝加入请求失败: {decline_error}")

                # ✅ 清除验证状态（CAS 防旧协程删新 session）
                await verification_service.clear_verification(
                    chat_id, user_id, expected=clear_token
                )

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


async def prepare_verification_challenge(group, chat_id: int, user_id: int) -> PreparedChallenge:
    """按群配置执行纯 prepare；random 在 service 内解析为具体类型，不写正式 Redis 键。"""
    return await VerificationService.prepare_challenge(group.verification_type, chat_id, user_id)


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


async def _start_initial_verification(
    bot: Bot,
    group,
    chat_id: int,
    user_id: int,
    username: str,
    flow: VerificationFlow,
) -> InitialDeliveryResult:
    """统一执行初始发送：reserve → prepare → commit → 启 timeout → send → promote。

    commit 成功后立即创建 timeout task（携带 session_id），这样 Telegram send 卡住或越过
    deadline 时 timeout 仍能 claim pending session。Forbidden 时 release 为 undelivered
    （保留状态供 /start 恢复）；其他发送错误 release 删全部状态。
    """
    verification_service = VerificationService()
    session_id = new_session_id()
    timeout = group.verification_timeout

    reservation = await reserve_initial_recovery(chat_id, user_id, session_id, timeout * 1000)
    if reservation is None:
        # 残留旧会话（inflight 锁正常时不该发生）；交给旧会话 timeout 兜底
        logger.warning(f"初始验证发送权竞争失败 [群组:{chat_id}] [用户:{user_id}]")
        return "busy"

    try:
        prepared = await prepare_verification_challenge(group, chat_id, user_id)
        committed = await verification_service.commit_challenge(
            chat_id,
            user_id,
            prepared,
            session_id,
            reservation.deadline_ms,
            flow,
            reservation=reservation,
        )
        if not committed:
            # commit 失败（reservation 过期或状态被替换）：抛异常让调用方 except 执行确定的
            # ban/decline。initial reserve 成功后无旧 session timeout 兜底，return busy 会被
            # 忽略导致用户永久受限。release 由本函数 except 统一执行。
            raise RuntimeError("commit_challenge 失败：reservation 已过期或状态被替换")

        # commit 成功即启 timeout（session_id 关联），即使后续 send 卡住也能 claim
        timeout_handler = (
            handle_join_request_timeout if flow == "join_request" else handle_verification_timeout
        )
        asyncio.create_task(
            timeout_handler(bot, chat_id, user_id, session_id=session_id, timeout=timeout)
        )

        try:
            sent_message = await send_verification_message(
                bot,
                chat_id,
                user_id,
                prepared.challenge,
                flow=flow,
                username=username,
                timeout=timeout,
            )
        except TelegramForbiddenError:
            # 用户未启动 Bot：release 为 undelivered，保留状态供 /start 恢复
            released = await release_recovery(reservation, preserve_challenge=True)
            return "undelivered" if released else "busy"

        if not await promote_recovery(reservation, flow, sent_message.message_id):
            # reservation 在发送期间失效（过期/被替换）：删未受状态机管理的 UI
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=sent_message.message_id)
            return "busy"

        return "sent"
    except Exception:
        # 未预期异常：release 删全部状态，避免残留不可恢复的 pending
        with contextlib.suppress(Exception):
            await release_recovery(reservation, preserve_challenge=False)
        raise


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
            await send_verification_success_message(
                bot, user_id, chat_title, "success_join_request", group_chat_id=chat_id
            )
        return

    # 批准失败（重试耗尽）：保留 approved_key 以便用户重新提交加入请求时自动批准
    with contextlib.suppress(Exception):
        await send_verification_success_message(
            bot, user_id, chat_title, "approve_failed", group_chat_id=chat_id
        )


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

        try:
            result = await _start_initial_verification(
                bot, group, chat_id, user_id, username, "join_request"
            )
            if result == "sent":
                logger.info(f"已向用户 {user_id} 私聊发送加入请求验证消息")
            elif result == "undelivered":
                # 用户未启动 Bot：状态已 release 为 undelivered，发布共享引导消息
                logger.warning(f"用户 {user_id} 未启动 Bot，加入请求验证等待恢复")
                await handle_user_not_started_bot_for_join_request(bot, chat_id, user_id)
        except Exception as e:
            logger.error(f"发送私聊验证消息失败: {e}")
            # 拒绝加入请求（状态已由 _start_initial_verification release 清理）
            await decline_join_request(bot, chat_id, user_id)

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
            await send_verification_success_message(
                bot, user_id, chat_title, "restore_failed", group_chat_id=chat_id
            )
        return

    # 恢复成功：清除验证标记
    await get_redis().delete(approved_key)

    # 在私聊中通知用户
    with contextlib.suppress(Exception):
        await send_verification_success_message(
            bot, user_id, chat_title, "success", group_chat_id=chat_id
        )

    # 在群内发送欢迎消息（按群 locale）
    welcome_msg = await send_group_welcome(bot, chat_id, user)

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
                f"用户 {user_id} 由管理员 {inviter_name} ({inviter_id}) 邀请，跳过验证直接通过"
            )

            # ✅ 清除可能存在的待验证状态（管理员批准加入请求场景）
            if await verification_service.is_verification_pending(chat_id, user_id):
                await verification_service.clear_verification(chat_id, user_id)
                logger.info(f"用户 {user_id} 由管理员邀请，已清除待验证状态")

            # 消费可能残留的 approved_key（管理员邀请/批准与 Bot 验证竞争时），使命已完成
            await redis.delete(RedisKeys.verification_approved(chat_id, user_id))

            # 直接发送欢迎消息（不需要限制权限，按群 locale）
            group_locale = await get_resolver().for_group(chat_id)
            localizer = get_translator().for_locale(group_locale)
            welcome_text = (
                localizer.t("verification.join.group.welcome", user=format_user_mention(user))
                + "\n\n"
                + localizer.t(
                    "verification.join.group.invited_by.message",
                    inviter=format_trusted_user_mention(event.from_user),
                )
            )
            welcome_msg = await bot.send_message(chat_id=chat_id, text=welcome_text)

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

            # 发送群内通知（30 秒后自动删除）；locale 解析在 try 内，失败只影响通知
            try:
                ban_notify_locale = await get_resolver().for_group(chat_id)
                ban_notify_localizer = get_translator().for_locale(ban_notify_locale)
                notify_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=ban_notify_localizer.t(
                        "verification.join.cas_ban.notify",
                        user=format_user_mention(user),
                    ),
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

            # 发送群内通知（30 秒后自动删除）；locale 解析在 try 内，失败只影响通知
            try:
                ban_notify_locale = await get_resolver().for_group(chat_id)
                ban_notify_localizer = get_translator().for_locale(ban_notify_locale)
                # reason → catalog key 显式映射，未知值统一 unknown.label
                # （防脏值/未来新增状态拼出不存在的 key）
                status_label_key_map = {
                    "restricted": "verification.join.status_ban.restricted.label",
                    "scam": "verification.join.status_ban.scam.label",
                    "fake": "verification.join.status_ban.fake.label",
                    "deleted": "verification.join.status_ban.deleted.label",
                }
                status_label_key = status_label_key_map.get(
                    status_result.reason or "",
                    "verification.join.status_ban.unknown.label",
                )
                status_text = ban_notify_localizer.t(status_label_key)
                notify_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=ban_notify_localizer.t(
                        "verification.join.status_ban.notify",
                        user=format_user_mention(user),
                        status=status_text,
                    ),
                )
                await auto_delete_message(notify_msg, delay=30)
            except Exception as e:
                logger.warning(f"发送封禁通知失败: {e}")

            logger.info(
                f"异常用户加入被拒 [群组:{chat_id}] [用户:{user_id}] [状态:{status_result.reason}]"
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

        try:
            result = await _start_initial_verification(
                bot, group, chat_id, user_id, username, "join"
            )
            if result == "sent":
                logger.info(f"已向用户 {user_id} 私聊发送验证消息")
            elif result == "undelivered":
                # 用户未启动 Bot：状态已 release 为 undelivered，发布共享引导消息
                logger.warning(f"用户 {user_id} 未启动 Bot，无法发送私聊验证")
                await handle_user_not_started_bot(bot, chat_id, user_id)
        except Exception as e:
            logger.error(f"发送私聊验证消息失败: {e}")
            # 踢出并封禁 1 小时（状态已由 _start_initial_verification release 清理）
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
    """callback_data 无法定位来源群时，仍尊重点击者的显式语言偏好"""
    locale = await get_resolver().for_user(callback.from_user.id)
    localizer = get_translator().for_locale(locale)
    await callback.answer(localizer.t(key), show_alert=True)


# callback_data 前缀 → Redis stored challenge_type（校验防跨类型旧消息重放）
_CHOICE_TYPES: dict[str, str] = {
    "verify_math": "math",
    "verify_slider": "slider",
    "verify_qa": "qa",
    "verify_emoji": "emoji",
    "verify_honeypot": "honeypot",
    "verify_puzzle": "puzzle",
}

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
        expected_type = _CHOICE_TYPES[prefix]
    except (KeyError, TypeError, ValueError):
        await _answer_default_toast(callback, "verification.callback.invalid_data.toast")
        return

    try:
        # not_yours：先身份判断，toast 用点击者显式偏好（避免提前查询目标 user locale）
        if callback.from_user.id != user_id:
            clicker_locale = await get_resolver().for_user(callback.from_user.id)
            clicker_localizer = get_translator().for_locale(clicker_locale)
            await callback.answer(
                clicker_localizer.t("verification.callback.not_yours.toast"), show_alert=True
            )
            return

        # 身份确认后才解析 owner 私聊 locale（选项 B：用户偏好 → 来源群）
        private_locale = await get_resolver().for_private_from_group(
            user_id=user_id, group_chat_id=chat_id
        )
        localizer = get_translator().for_locale(private_locale)

        # 一次 GET 校验 pending + 类型 + 答案（消除 TOCTOU，防跨类型旧消息重放）
        verification_service = VerificationService()
        # 副作用前捕获快照：wrong 路径 ban 经网络，clear 须用旧 session 快照防删新 session
        clear_token = await verification_service.capture_clear_token(chat_id, user_id)
        answer_result = await verification_service.verify_choice_answer(
            chat_id, user_id, expected_type, answer
        )
        if answer_result == "expired":
            await callback.answer(
                localizer.t("verification.callback.expired.toast"), show_alert=False
            )
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            return

        if answer_result == "correct":
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
            else:
                # 踢出并封禁 1 小时
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=datetime.now() + timedelta(hours=1),
                )

            # ✅ 清除验证状态（CAS 防旧协程删新 session；type 随 clear 原子删，不单独 DEL）
            await verification_service.clear_verification(chat_id, user_id, expected=clear_token)

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
    # 点击者显式语言偏好（身份确认前的 toast + 异常兜底）
    clicker_locale = await get_resolver().for_user(callback.from_user.id)
    clicker_localizer = get_translator().for_locale(clicker_locale)

    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                clicker_localizer.t("verification.callback.invalid_data.toast"),
                show_alert=True,
            )
            return

        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer(
                clicker_localizer.t("verification.callback.not_yours.toast"),
                show_alert=True,
            )
            return

        # 身份确认后解析 owner 私聊 locale（选项 B：用户偏好 → 来源群）
        private_locale = await get_resolver().for_private_from_group(
            user_id=user_id, group_chat_id=chat_id
        )
        localizer = get_translator().for_locale(private_locale)

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

        await callback.answer(
            localizer.t("verification.captcha.input_prompt.toast"), show_alert=False
        )

    except Exception as e:
        logger.error(f"处理验证码输入请求失败: {e}")
        await callback.answer(
            clicker_localizer.t("verification.callback.failed.toast"), show_alert=True
        )


@router.callback_query(F.data.startswith("verify_captcha_refresh:"))
async def on_captcha_refresh(callback: CallbackQuery, bot: Bot) -> None:
    """处理验证码刷新 - 私聊模式"""
    # 点击者显式语言偏好（身份确认前的 toast）
    clicker_locale = await get_resolver().for_user(callback.from_user.id)
    clicker_localizer = get_translator().for_locale(clicker_locale)

    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                clicker_localizer.t("verification.callback.invalid_data.toast"),
                show_alert=True,
            )
            return

        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer(
                clicker_localizer.t("verification.callback.not_yours.toast"),
                show_alert=True,
            )
            return

        # 身份确认后解析 owner 私聊 locale（选项 B：用户偏好 → 来源群）
        private_locale = await get_resolver().for_private_from_group(
            user_id=user_id, group_chat_id=chat_id
        )
        localizer = get_translator().for_locale(private_locale)

        # 重新生成验证码
        verification_service = VerificationService()
        username = callback.from_user.full_name

        # 获取群组配置获取超时时间
        group_repo = GroupRepository()
        group_config = await group_repo.get(chat_id)
        timeout = group_config.verification_timeout if group_config else 120

        # 刷新需独立 CAS 提交（不能用旧 SETEX，会在 clear/timeout 后复活状态）：
        # 仅当验证仍在进行时 prepare + commit_captcha_refresh 原子替换答案
        if not await verification_service.is_verification_pending(chat_id, user_id):
            await callback.answer(
                localizer.t("verification.callback.expired.toast"), show_alert=False
            )
            return

        prepared = await verification_service.prepare_challenge("captcha", chat_id, user_id)
        if not isinstance(prepared.challenge, CaptchaChallenge):
            raise TypeError("captcha prepare 返回了错误的 challenge 类型")
        challenge = prepared.challenge

        # 刷新后仅更新题面与按钮（保持原行为：不重复信封标题），按 locale 渲染
        rendered = render_captcha_for_refresh(
            challenge, localizer, chat_id, user_id, username, timeout
        )

        # 检查 photo 是否存在
        if rendered.photo is None:
            await callback.answer(
                localizer.t("verification.captcha.generate_failed.toast"), show_alert=True
            )
            return

        # 先编辑（media + caption 一步原子，避免「media 新 caption 旧」）；失败则 except 不 commit
        await bot.edit_message_media(
            chat_id=user_id,
            message_id=callback.message.message_id,
            media=InputMediaPhoto(media=rendered.photo, caption=rendered.text),
            reply_markup=(
                rendered.keyboard if isinstance(rendered.keyboard, InlineKeyboardMarkup) else None
            ),
        )

        # 编辑成功后 CAS commit（仅当 captcha session 仍有效才替换答案；timeout 已 claim 则失败）
        if not await verification_service.commit_captcha_refresh(chat_id, user_id, prepared):
            await callback.answer(
                localizer.t("verification.callback.expired.toast"), show_alert=False
            )
            return
        await callback.answer(localizer.t("verification.captcha.refreshed.toast"), show_alert=False)

    except Exception as e:
        logger.error(f"处理验证码刷新失败: {e}")
        await callback.answer(
            clicker_localizer.t("verification.callback.failed.toast"), show_alert=True
        )


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

        # 解析 owner 私聊 locale（选项 B：用户偏好 → 来源群）
        private_locale = await get_resolver().for_private_from_group(
            user_id=user_id, group_chat_id=chat_id
        )
        localizer = get_translator().for_locale(private_locale)

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

        # 副作用前捕获快照（wrong 路径 ban 经网络，clear 须用旧 session 快照防删新 session）
        clear_token = await verification_service.capture_clear_token(chat_id, user_id)
        # 验证答案（正确时内部已原子 claim，与 timeout 互斥）
        answer_result = await verification_service.verify_answer(chat_id, user_id, text_input)

        if answer_result == "expired":
            # timeout 已 claim 或 session 已切换：不恢复权限，也不执行失败处罚
            await redis.delete(waiting_key)
            await redis.delete(waiting_user_key)  # ✅ 删除反向索引
            return

        if answer_result == "correct":
            # claim_success 已清 main/deadline/token；这里清 captcha 输入辅助键
            await redis.delete(waiting_key)
            await redis.delete(waiting_user_key)  # ✅ 删除反向索引

            # 检查验证类型（claim 保留 type 供此处读取 flow）
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            # 删除验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=int(message_id_str))

            await message.answer(localizer.t("verification.captcha.input_success.private.message"))

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
                            bot,
                            user_id,
                            chat_title,
                            "approve_failed",
                            group_chat_id=chat_id,
                        )
                    return
                logger.info(f"用户 {user_id} 验证成功，已批准加入请求")
            else:
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
                    return

                # 恢复成功：approved_key 使命完成，删除避免 10 分钟内重新加入免验证
                await redis.delete(approved_key)

                # 发送欢迎消息到群组（masked 可点击 mention，按群 locale 渲染）
                assert message.from_user  # 类型缩小
                welcome_msg = await send_group_welcome(
                    bot, chat_id, message.from_user, mention_style="linked"
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
            await message.answer(localizer.t("verification.captcha.input_wrong.private.message"))
            await redis.delete(waiting_key)
            await redis.delete(waiting_user_key)  # ✅ 删除反向索引

            # 根据验证类型决定踢出或拒绝
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)

            if verification_type == "join_request":
                # 拒绝加入请求
                await decline_join_request(bot, chat_id, user_id)
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

            # ✅ 清除验证状态（CAS 防旧协程删新 session；type 随 clear 原子删）
            await verification_service.clear_verification(chat_id, user_id, expected=clear_token)

            logger.info(f"用户 {user_id} 验证码验证失败")

    except Exception as e:
        logger.error(f"处理验证码文本输入失败: {e}")


@router.callback_query(F.data.startswith("verify_cancel:"))
async def on_verify_cancel(callback: CallbackQuery, bot: Bot) -> None:
    """处理取消验证 - 私聊模式"""
    # 点击者显式语言偏好（身份确认前的 toast + 异常兜底）
    clicker_locale = await get_resolver().for_user(callback.from_user.id)
    clicker_localizer = get_translator().for_locale(clicker_locale)

    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                clicker_localizer.t("verification.callback.invalid_data.toast"),
                show_alert=True,
            )
            return

        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer(
                clicker_localizer.t("verification.callback.not_yours.toast"),
                show_alert=True,
            )
            return

        # 身份确认后解析 owner 私聊 locale（选项 B：用户偏好 → 来源群）
        private_locale = await get_resolver().for_private_from_group(
            user_id=user_id, group_chat_id=chat_id
        )
        localizer = get_translator().for_locale(private_locale)

        # ✅ 检查验证状态是否还存在
        verification_service = VerificationService()
        if not await verification_service.is_verification_pending(chat_id, user_id):
            await callback.answer(
                localizer.t("verification.callback.expired.toast"), show_alert=False
            )
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            return

        # 副作用前捕获快照（ban 经网络，clear 须用旧 session 快照防删新 session）
        clear_token = await verification_service.capture_clear_token(chat_id, user_id)

        # 踢出并封禁 1 小时
        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=datetime.now() + timedelta(hours=1),
        )

        # 删除私聊中的验证消息
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)

        # 清除验证状态（CAS 防旧协程删新 session）
        await verification_service.clear_verification(chat_id, user_id, expected=clear_token)

        await callback.answer(localizer.t("verification.callback.cancelled.toast"))
        logger.info(f"用户 {user_id} 取消验证，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理取消验证失败: {e}")
        with contextlib.suppress(Exception):
            await callback.answer(
                clicker_localizer.t("verification.callback.failed.toast"), show_alert=True
            )


@router.message(F.web_app_data)
async def on_webapp_data(message: Message, bot: Bot) -> None:
    """处理 WebApp 返回的数据（所有 CAPTCHA 验证回调）"""
    import hashlib
    import hmac
    import json
    import time

    from src.core.config import settings

    # 类型检查（缓存 from_user / from_user_id 局部变量，使闭包与异常路径类型安全）
    from_user = message.from_user
    if not from_user or not message.web_app_data:
        return
    from_user_id = from_user.id

    # 鉴权前错误通知给真实发送者（from_user 由 Telegram 协议保证可信；
    # payload user_id 在身份校验通过前不可信，不可用于通知或状态变更）
    clicker_locale = await get_resolver().for_user(from_user_id)
    clicker_localizer = get_translator().for_locale(clicker_locale)

    async def send_error(key: str) -> None:
        """向真实发送者发送验证错误消息（HTML）"""
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=from_user_id,
                text=clicker_localizer.t(key),
                parse_mode="HTML",
            )

    # 记录收到 WebApp 数据（不记录原始/解析后 payload，token/signature 可能泄露）
    logger.info(
        f"收到 WebApp 数据 [from_user:{from_user_id}] "
        f"[data_length:{len(message.web_app_data.data)}]"
    )

    try:
        data = json.loads(message.web_app_data.data)

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

        # ✅ 身份校验：message.from_user.id 由 Telegram 协议保证可信（WebApp sendData
        # 的 from 不可伪造），是比 payload user_id 更强的身份断言。payload user_id 来自
        # WebApp URL param（非 initData 可信源），攻击者可篡改。不符则拒绝，不操作 token/状态。
        if from_user_id != user_id:
            logger.warning(
                f"WebApp 用户身份不匹配 [from_user:{from_user_id}] "
                f"[payload_user:{user_id}] [chat:{chat_id}]"
            )
            await send_error("verification.webapp.error.generic.private.message")
            return

        verify_token = data["verify_token"]
        signature = data["signature"]
        timestamp = int(data["timestamp"])

        logger.info(f"收到 {provider.upper()} WebApp 回调 [user:{user_id}] [chat:{chat_id}]")

        # 1. 验证时间戳（防止重放，5 分钟内有效）
        if abs(time.time() - timestamp) > 300:
            logger.warning(f"{provider.upper()} 回调时间戳过期 [user:{user_id}]")
            await send_error("verification.webapp.error.expired.private.message")
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
            await send_error("verification.webapp.error.signature.private.message")
            return

        # 3. 验证 Redis 中的 token 存在（一次性）
        redis = get_redis()
        verification_service = VerificationService()
        # 副作用前捕获快照：token 校验后 CAS clear 须用旧 session 快照，防 timeout/新 session/
        # 并发重复回调胜出时仍恢复权限（P2.1 + webapp 竞态）
        clear_token = await verification_service.capture_clear_token(chat_id, user_id)

        # 统一使用 captcha_token 键
        token_key = RedisKeys.captcha_token(chat_id, user_id)
        stored_token_data = await redis.get(token_key)

        # 检查 token
        if not stored_token_data:
            logger.warning(
                f"CAPTCHA 验证 token 不存在或已过期 [provider:{provider}] [user:{user_id}]"
            )
            await send_error("verification.webapp.error.invalid_token.private.message")
            return

        # 解析 token（统一格式：provider:token[:key_index]）
        token_parts = stored_token_data.split(":")
        if len(token_parts) < 2 or token_parts[0] != provider:
            logger.warning(f"CAPTCHA 验证 token 格式错误 [provider:{provider}] [user:{user_id}]")
            await send_error("verification.webapp.error.invalid_token.private.message")
            return

        stored_token = token_parts[1]

        if stored_token != verify_token:
            logger.warning(
                f"{provider.upper()} token 不匹配 [user:{user_id}] [stored:{stored_token[:8]}...] [got:{verify_token[:8]}...]"
            )
            await send_error("verification.webapp.error.invalid_token.private.message")
            return

        # 4. 检查验证类型；token/type 由下面的 CAS clear 原子消费（不单独 DEL，防删新 session）
        type_key = RedisKeys.verification_type(chat_id, user_id)
        verification_type = await redis.get(type_key)
        is_join_request = verification_type == "join_request"

        # ✅ 5. CAS 消费当前 session；timeout 已 claim / 新 session 建立 / 并发重复回调胜出时
        # clear 失败，必须停止（不恢复权限，否则与 timeout 重复或对错 session 操作）
        cleared = await verification_service.clear_verification(
            chat_id, user_id, expected=clear_token
        )
        if not cleared:
            logger.info(
                f"忽略已失效的 WebApp 回调 [provider:{provider}] [user:{user_id}] [chat:{chat_id}]"
            )
            await send_error("verification.webapp.error.expired.private.message")
            return
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
            message_type: SuccessMessageType = (
                "success_join_request" if approved else "approve_failed"
            )
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"
                await send_verification_success_message(
                    bot, user_id, chat_title, message_type, group_chat_id=chat_id
                )

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
                        bot,
                        user_id,
                        chat_title,
                        "restore_failed",
                        group_chat_id=chat_id,
                    )
                return  # 提前返回，不发送欢迎消息

            # 恢复成功：approved_key 使命完成，删除避免 10 分钟内重新加入免验证
            await redis.delete(approved_key)

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"
                await send_verification_success_message(
                    bot, user_id, chat_title, "success", group_chat_id=chat_id
                )

            # 在群内发送欢迎消息（plain 脱敏 mention，按群 locale 渲染）
            welcome_msg = await send_group_welcome(bot, chat_id, from_user)

            # 5秒后删除欢迎消息
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

            logger.info(f"用户 {user_id} {provider.upper()} 验证成功 (群组 {chat_id})")

    except Exception:
        # 不依据未鉴权 payload 修改任何验证状态：异常若发生在 clear_verification 之前，
        # 由 timeout 任务兜底；发生在之后则状态已清。原「按 payload 清状态」可被构造为
        # 跨用户 DoS（攻击者塞入受害者 chat/user_id 再触发异常），已移除。
        logger.exception(f"处理 CAPTCHA 回调失败 [from_user:{from_user_id}]")
        await send_error("verification.webapp.error.generic.private.message")


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

        # claim_success 已在 verify_choice_answer 内原子消费状态（main/deadline/token 删除，
        # recovery=success）；此处不再 clear，避免误删后续新会话

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
            welcome_msg = await send_group_welcome(bot, chat_id, callback.from_user)

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


async def _wait_for_timeout_claim(
    chat_id: int,
    user_id: int,
    session_id: str | None,
    flow: VerificationFlow,
) -> tuple[int, VerificationClearToken] | None:
    """等待 Redis deadline 并 claim；返回 (message_id, clear_token)，None 表示 session 已失效。

    clear_token 是 claim 时的状态快照（claim 原子返回），供 ban/decline 网络后 clear CAS，
    防旧 timeout 协程删新 session。旧的无 session_id 调用 fail-closed 直接返回 None——退化到
    is_verification_pending 会让旧 timeout 误罚新 session。timeout 始终以 Redis deadline 为准。
    """
    if not session_id:
        logger.warning(f"忽略缺少 session_id 的旧 timeout 任务 [群组:{chat_id}] [用户:{user_id}]")
        return None

    while True:
        claim = await claim_timeout(chat_id, user_id, session_id, flow)
        if claim.status == "stale":
            return None
        if claim.status == "claimed":
            if claim.clear_token is None:
                logger.error(f"timeout claim 缺少 clear token [群组:{chat_id}] [用户:{user_id}]")
                return None
            return claim.message_id, claim.clear_token
        # wait：按 Redis 剩余毫秒重排（恢复可能延长了 deadline）
        await asyncio.sleep(max(0.001, claim.remaining_ms / 1000))


async def handle_verification_timeout(
    bot: Bot,
    chat_id: int,
    user_id: int,
    *,
    session_id: str | None,
    timeout: int,
) -> None:
    """处理验证超时 - 私聊验证模式（join flow）"""
    try:
        timeout_claim = await _wait_for_timeout_claim(chat_id, user_id, session_id, "join")
        if timeout_claim is None:
            return
        message_id, clear_token = timeout_claim

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

        # 2. 删除私聊中的验证消息（message_id=0 表示无 UI，如 hint 路径）
        if message_id > 0:
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=message_id)

        # 3. 在私聊中通知用户（可选）
        with contextlib.suppress(Exception):
            chat = await bot.get_chat(chat_id)
            chat_title = escape_html(chat.title) if chat.title else "群组"
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
                parse_mode="HTML",
            )

        # 4. 群内不发送任何消息

        # 5. 清除验证状态（claim 已删主键/token，此处清剩余键；CAS 防旧协程删新 session）
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id, expected=clear_token)

        logger.info(f"用户 {user_id} 验证超时处理完成（已踢出+封禁1小时）")

    except Exception as e:
        logger.error(f"处理验证超时失败: {e}")


async def _send_hint_message(
    bot: Bot,
    chat_id: int,
    flow: VerificationHintFlow,
) -> Message:
    """按群 locale 发送对应 flow 的验证引导消息（未启动 Bot 提示）。

    chat_title 获取失败时回退到语言无关的 chat_id，避免重新引入硬编码中文。
    """
    group_locale = await get_resolver().for_group(chat_id)
    localizer = get_translator().for_locale(group_locale)

    bot_info = await bot.get_me()
    bot_username = bot_info.username

    chat_title = str(chat_id)
    with contextlib.suppress(Exception):
        chat = await bot.get_chat(chat_id)
        chat_title = chat.title or chat_title

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t("verification.hint.start.button"),
                    url=(
                        f"https://t.me/{bot_username}?start="
                        f"{'verify_join_request_' if flow == 'join_request' else 'verify_'}{chat_id}"
                    ),
                )
            ]
        ]
    )

    return await bot.send_message(
        chat_id=chat_id,
        text=localizer.t(
            f"verification.hint.{flow}.group.message",
            chat_title=escape_html(chat_title),
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def _publish_shared_hint(
    bot: Bot,
    chat_id: int,
    flow: VerificationHintFlow,
) -> None:
    """竞争发送权并发布共享引导消息（NX reservation → send → promote CAS）。

    取不到发送权时尝试延长已提交消息的共享窗口。发送失败或 reservation 过期时
    清理自己的 reservation / 未提交消息，避免第二条 hint 残留。
    """
    owner_token = await reserve_hint(chat_id, flow)
    if owner_token is None:
        # 已有 hint（已提交或他人 pending）：已提交则延长共享窗口；pending 不续命
        if await try_extend_hint(chat_id, flow):
            logger.debug(f"群组 {chat_id} 已有 {flow} 引导消息，延长 TTL 到 30 秒")
        return

    try:
        hint_msg = await _send_hint_message(bot, chat_id, flow)
        if await promote_hint(chat_id, flow, owner_token, hint_msg.message_id):
            asyncio.create_task(
                delete_hint_message_after_delay(bot, chat_id, hint_msg.message_id, flow, 30)
            )
            logger.info(f"群组 {chat_id} 发送 {flow} 验证引导消息（30秒内共享）")
        else:
            # reservation 在发送期间过期或被替换：删未受状态机管理的消息，不覆盖新 owner
            logger.warning(
                f"群组 {chat_id} {flow} 引导 reservation 已失效，删除未提交消息 "
                f"{hint_msg.message_id}"
            )
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=hint_msg.message_id)
    except Exception:
        with contextlib.suppress(Exception):
            await delete_hint_reservation(chat_id, flow, owner_token)
        logger.error(f"发送 {flow} 引导消息失败", exc_info=True)


async def handle_user_not_started_bot(bot: Bot, chat_id: int, user_id: int) -> None:
    """直接入群用户未启动 Bot：发布共享引导消息。

    challenge 已由 _start_initial_verification 标记为 undelivered，并由同 session 的
    timeout task 兜底处罚，故此处不再另启 timeout。30 秒内同一 flow 只发一条引导消息。
    """
    await _publish_shared_hint(bot, chat_id, "join")
    logger.info(f"用户 {user_id} 的 join challenge 已标记为 undelivered（群组 {chat_id}）")


async def delete_hint_message_after_delay(
    bot: Bot,
    chat_id: int,
    message_id: int,
    flow: VerificationHintFlow,
    delay: int,
) -> None:
    """延迟删除引导消息。

    支持共享窗口延长：只有 key 仍指向当前消息时才跟随其 TTL，避免旧删除任务
    被同 flow 的新 reservation 或新消息拖延。删除任务不调用 try_extend_hint，
    否则会自行续期导致 hint 永不删除。
    """
    try:
        await asyncio.sleep(delay)

        # 原子校验：仅当 hint 仍指向当前 message_id 时按其剩余 TTL 拖延删除，
        # 避免旧消息按新 reservation 的 TTL 被拖延、新旧两条 hint 共存（codex P2）
        remaining_ttl = await get_hint_ttl_if_match(chat_id, flow, message_id)
        if remaining_ttl > 0:
            logger.debug(
                f"群组 {chat_id} 的 {flow} 引导消息 TTL 被延长，继续等待 {remaining_ttl} 秒"
            )
            asyncio.create_task(
                delete_hint_message_after_delay(bot, chat_id, message_id, flow, remaining_ttl)
            )
            return

        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"已删除群组 {chat_id} 的 {flow} 引导消息 {message_id}")
    except Exception as e:
        logger.error(f"延迟删除引导消息失败: {e}")


async def handle_join_request_timeout(
    bot: Bot,
    chat_id: int,
    user_id: int,
    *,
    session_id: str | None,
    timeout: int,
) -> None:
    """处理加入请求验证超时 - 拒绝加入请求（join_request flow）"""
    try:
        timeout_claim = await _wait_for_timeout_claim(chat_id, user_id, session_id, "join_request")
        if timeout_claim is None:
            return
        message_id, clear_token = timeout_claim

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

        # 3. 删除私聊中的验证消息（message_id=0 表示无 UI）
        if message_id > 0:
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=message_id)

        # 4. 在私聊中通知用户
        with contextlib.suppress(Exception):
            chat = await bot.get_chat(chat_id)
            chat_title = escape_html(chat.title) if chat.title else "群组"
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

        # 5. 清除验证状态（claim 已删主键/token，此处清剩余键；CAS 防旧协程删新 session）
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id, expected=clear_token)

        logger.info(f"用户 {user_id} 加入请求验证超时处理完成（已拒绝+封禁1小时）")

    except Exception as e:
        logger.error(f"处理加入请求验证超时失败: {e}")


async def handle_user_not_started_bot_for_join_request(
    bot: Bot, chat_id: int, user_id: int
) -> None:
    """加入请求用户未启动 Bot：发布共享引导消息，保留 undelivered 供 /start 恢复。

    challenge 已由 _start_initial_verification 标记为 undelivered（保留主键/deadline/type），
    由同 session 的 timeout task 兜底拒绝+封禁。4b /start 恢复入口已接入，用户点 hint
    按钮 → /start verify_join_request_{chat_id} → handle_verification_start 恢复 challenge。
    """
    await _publish_shared_hint(bot, chat_id, "join_request")
    logger.info(f"用户 {user_id} 的 join_request challenge 已标记为 undelivered（群组 {chat_id}）")


def _recovery_branch(recovery: str | None) -> str:
    """解析 recovery 值的状态段：undelivered/pending/message/timeout/none。"""
    if not recovery:
        return "none"
    if recovery.startswith("undelivered:"):
        return "undelivered"
    if recovery.startswith("pending:"):
        return "pending"
    if recovery.startswith("message:"):
        return "message"
    if recovery.startswith("timeout:"):
        return "timeout"
    return "none"


async def handle_verification_start(
    message: Message,
    bot: Bot,
    chat_id: int,
    flow_hint: VerificationFlow,
) -> None:
    """/start verify_[join_request_]{chat_id}：按成员状态 + recovery 状态恢复 challenge UI。

    成员矩阵区分 join / join_request 两 flow（join_request + left 是合法申请中状态，
    不按 left=stale 清理）。recovery 为 undelivered 时走恢复链路；其余按状态提示。
    flow 以 verification_type 键为权威（recovery 在时），deep-link flow_hint 仅兼容旧链接。
    """
    if not message.from_user:
        return
    user_id = message.from_user.id

    locale = await get_resolver().for_private_from_group(user_id=user_id, group_chat_id=chat_id)
    localizer = get_translator().for_locale(locale)
    chat_title = str(chat_id)
    with contextlib.suppress(Exception):
        chat = await bot.get_chat(chat_id)
        chat_title = chat.title or chat_title
    title_html = escape_html(chat_title)

    def _answer(key: str, **kwargs: object):
        text = localizer.t(
            f"verification.start.{key}.private.message", chat_title=title_html, **kwargs
        )
        return message.answer(text, parse_mode="HTML")

    # 成员状态查询（失败不读写状态，仅提示稍后重试）
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        logger.warning(f"/start 恢复查询成员状态失败 [群组:{chat_id}] [用户:{user_id}]")
        await _answer("member_lookup_failed")
        return

    # 管理员/创建者：无需验证，清 stale
    if member.status in ("administrator", "creator"):
        await VerificationService().clear_verification(chat_id, user_id)
        await _answer("admin")
        return

    redis = get_redis()
    recovery = await redis.get(RedisKeys.verification_recovery(chat_id, user_id))
    type_value = await redis.get(RedisKeys.verification_type(chat_id, user_id))
    main_value = await redis.get(RedisKeys.verification(chat_id, user_id))
    # flow 以 verification_type 键为权威（join/join_request 均覆盖，防 flow_hint 误点跨 flow
    # 导致 reserve 用错误 flow、commit 因 type 不匹配失败而误删原会话）；flow_hint 仅在 type
    # 缺失（无 recovery，兼容旧链接）时使用
    flow: VerificationFlow = type_value if type_value in ("join", "join_request") else flow_hint
    branch = _recovery_branch(recovery)
    is_member = member.status == "member" or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )
    # 可恢复：undelivered，或 pending 过期后 recovery 缺失但主键仍在（reserve 支持 recovery nil）
    recoverable = branch == "undelivered" or (branch == "none" and main_value is not None)

    if flow == "join":
        if not is_member:
            # left/kicked/restricted(非 member)：清 stale + 提示重新加入
            await VerificationService().clear_verification(chat_id, user_id)
            await _answer("rejoin")
            return
        if recoverable:
            await _recover_verification_challenge(message, bot, chat_id, user_id, flow, _answer)
            return
        if branch == "pending":
            await _answer("recovering")
            return
        if branch == "message":
            await _answer("already_sent")
            return
        # none/timeout 且主键不在：无待验证（可能已完成）
        await _answer("no_pending.member")
        return

    # flow == "join_request"
    if is_member:
        # member/restricted(is_member=True)：已加入（加入请求已批准），无需验证
        await VerificationService().clear_verification(chat_id, user_id)
        await _answer("no_pending.member")
        return
    if member.status == "kicked":
        await VerificationService().clear_verification(chat_id, user_id)
        await _answer("resubmit_join_request")
        return
    # join_request + left/restricted(非 member)：合法申请中
    if recoverable:
        await _recover_verification_challenge(message, bot, chat_id, user_id, flow, _answer)
        return
    if branch == "pending":
        await _answer("recovering")
        return
    if branch == "message":
        await _answer("already_sent")
        return
    # none/timeout 且主键不在
    await _answer("resubmit_join_request")


async def _recover_verification_challenge(
    message: Message,
    bot: Bot,
    chat_id: int,
    user_id: int,
    flow: VerificationFlow,
    _answer: Callable[..., Awaitable[object]],
) -> None:
    """恢复链路：reserve_recovery → prepare → commit → send → promote。

    session/deadline 沿用原值（reserve_recovery 从 deadline 键读原 session），保证初始
    timeout task 的 session_id 匹配、claim 能读到恢复后的真实 message_id。不启新 timeout。
    """
    verification_service = VerificationService()
    redis = get_redis()
    revision = new_revision_id()

    # 先 reserve（原子读旧主键 + deadline），避免「单独 GET main → reserve」间的 TOCTOU
    reservation = await reserve_recovery(chat_id, user_id, revision)
    if reservation is None:
        # 并发 /start（busy）或状态已变（missing/expired）
        current = await redis.get(RedisKeys.verification_recovery(chat_id, user_id))
        if _recovery_branch(current) in ("pending", "undelivered"):
            await _answer("recovering")
        else:
            await _answer("recovery_failed")
        return

    # 从 reservation.expected_state_value（reserve 原子读的旧主键）解析 challenge type；
    # 剩余时间用 reservation.deadline_ms（reserve 原子读的原 deadline），避免再 GET 产生 TOCTOU
    if not reservation.expected_state_value or ":" not in reservation.expected_state_value:
        await release_recovery(reservation, preserve_challenge=True)
        await _answer("recovery_failed")
        return
    challenge_type = reservation.expected_state_value.split(":", 1)[0]
    remaining_seconds = (reservation.deadline_ms - int(time.time() * 1000)) // 1000
    if remaining_seconds <= 0:
        # deadline 已到（或不足 1 秒）：不发送立即过期的 UI，release 保留状态让 timeout 兜底
        await release_recovery(reservation, preserve_challenge=True)
        await _answer("expired")
        return

    try:
        prepared = await verification_service.prepare_challenge(challenge_type, chat_id, user_id)
        committed = await commit_recovery(
            reservation,
            state_value=prepared.state_value,
            auxiliary_state=prepared.auxiliary_state,
            flow=flow,
        )
        if not committed:
            # 保留 undelivered 让初始 timeout 兜底（preserve=False 会删 main/deadline，导致
            # timeout claim 拿到 stale，用户永久受限且加入请求永久未处理）
            await release_recovery(reservation, preserve_challenge=True)
            await _answer("recovery_failed")
            return

        username = message.from_user.full_name if message.from_user else ""
        try:
            sent = await send_verification_message(
                bot,
                chat_id,
                user_id,
                prepared.challenge,
                flow=flow,
                username=username,
                timeout=remaining_seconds,
            )
        except TelegramForbiddenError:
            # /start 已启动 Bot，Forbidden 不该发生；保守 release preserve + 失败提示
            await release_recovery(reservation, preserve_challenge=True)
            await _answer("recovery_failed")
            return

        if not await promote_recovery(reservation, flow, sent.message_id):
            # reservation 在发送期间失效：删未受状态机管理的 UI，release 保留状态给 timeout
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=sent.message_id)
            await release_recovery(reservation, preserve_challenge=True)
            await _answer("recovery_failed")
            return

        await _answer("recovered", timeout=remaining_seconds)
    except Exception:
        # 保留 undelivered 让初始 timeout 兜底（preserve=False 会删全部，取消 timeout 终态）
        with contextlib.suppress(Exception):
            await release_recovery(reservation, preserve_challenge=True)
        raise
