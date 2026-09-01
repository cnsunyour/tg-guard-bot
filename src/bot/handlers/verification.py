"""入群验证处理器"""

import asyncio
import contextlib
import math
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import timedelta
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
from redis.asyncio import Redis

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
from src.core.tasks import spawn_background_task
from src.core.utils import (
    anonymous_mentions_html,
    auto_delete_message,
    escape_html,
    format_trusted_user_mention,
    format_user_mention,
    masked_mention_html,
    utcnow,
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
    add_hint_user,
    claim_hint_edit,
    claim_hint_render,
    delete_hint_reservation,
    get_hint_ttl_if_match,
    promote_hint,
    reserve_hint,
    snapshot_hint_users,
    try_extend_hint,
)
from src.services.verification_recovery import (
    VerificationClearToken,
    claim_timeout,
    commit_recovery,
    new_revision_id,
    new_session_id,
    parse_deadline_value,
    promote_recovery,
    redis_text,
    release_recovery,
    reserve_initial_recovery,
    reserve_recovery,
)

router = Router(name="verification")

# 初始发送编排结果：sent 已发送 / undelivered 未启动 Bot（可恢复）/ busy 残留旧会话
type InitialDeliveryResult = Literal["sent", "undelivered", "busy"]

# 群内验证引导消息的共享窗口（秒）：窗口内同群同 flow 只发一条，到期删除。
# catalog 文案「此提示将在 30 秒后自动删除」与此值对应，调整时需同步三语文案。
_HINT_SHARE_WINDOW_SECONDS = 30

# 安全释放 in-flight 锁的 Lua 脚本：仅当键值等于 owner token 时才删除，
# 避免「单次处理耗时超过 TTL → 旧协程 finally 误删新协程刚取得的锁」。
_INFLIGHT_RELEASE_SCRIPT = (
    'if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) end return 0'
)

# 原子绑定当前 captcha session 与 recovery UI 到 waiting 键。
#
# GET deadline 再 SET waiting 有 TOCTOU；且旧验证消息的 callback_data 不含 session，若不校验
# recovery message_id，点击旧按钮会把旧 UI 错绑到新 session。Lua 内原子校验
# deadline/main(captcha:)/recovery(message:{session}:...:{message_id}) + recovery message_id
# == 点击消息 + deadline 未到，才 SET waiting `{session}:{message_id}` + waiting_user（PXAT deadline）。
#
# KEYS = [deadline, main, recovery, waiting, waiting_user]
# ARGV = [message_id, chat_id]
# 成功返回 "{session}:{message_id}"，失败返回 0。
_CAPTCHA_WAITING_SET_SCRIPT = """
local deadline_raw = redis.call("get", KEYS[1])
local state_raw = redis.call("get", KEYS[2])
local recovery_raw = redis.call("get", KEYS[3])
if not deadline_raw or not state_raw or not recovery_raw then
    return 0
end

local session, deadline_text = string.match(deadline_raw, "^([^:]+):(%d+)$")
local deadline_ms = tonumber(deadline_text)
if not session or not deadline_ms then
    return 0
end

if not string.match(state_raw, "^captcha:[^:]+$") then
    return 0
end

-- recovery 必须匹配当前 session：promote 后 message:{session}:...:{message_id}（校验
-- message_id == 点击消息），或 send→promote 窗口 pending:{session}:...（仅校验 session，
-- message_id 由随后的 promote 写入；该窗口极短，放宽 message_id 避免合法点击被判 expired）
local recovery_message_session = string.match(recovery_raw, "^message:([^:]+):[^:]+:[^:]+:%d+$")
if recovery_message_session then
    if recovery_message_session ~= session then
        return 0
    end
    local recovery_message_id = string.match(recovery_raw, ":(%d+)$")
    if recovery_message_id ~= ARGV[1] then
        return 0
    end
else
    local recovery_pending_session = string.match(recovery_raw, "^pending:([^:]+):[^:]+:[^:]+$")
    if not recovery_pending_session or recovery_pending_session ~= session then
        return 0
    end
end

local clock = redis.call("time")
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if now_ms >= deadline_ms then
    return 0
end

local waiting_value = session .. ":" .. ARGV[1]
redis.call("set", KEYS[4], waiting_value, "PXAT", deadline_ms)
redis.call("set", KEYS[5], ARGV[2], "PXAT", deadline_ms)
return waiting_value
""".strip()

# captcha waiting 的 compare-and-delete：仅删调用方实际读到的 waiting 值，防并发点击新 session
# 写入的值被旧协程误删。
#
# KEYS = [waiting, waiting_user]
# ARGV = [expected_waiting_value, expected_chat_id]
_CAPTCHA_WAITING_CLEAR_SCRIPT = """
if redis.call("get", KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call("del", KEYS[1])
if redis.call("get", KEYS[2]) == ARGV[2] then
    redis.call("del", KEYS[2])
end
return 1
""".strip()


async def _clear_captcha_waiting_if_match(
    chat_id: int,
    user_id: int,
    expected_waiting_value: str,
) -> bool:
    """仅清除调用方实际读取到的 waiting，避免误删并发写入的新 session 值。"""
    redis = get_redis()
    return bool(
        await redis.eval(
            _CAPTCHA_WAITING_CLEAR_SCRIPT,
            2,
            RedisKeys.captcha_waiting(chat_id, user_id),
            RedisKeys.captcha_waiting_user(user_id),
            expected_waiting_value,
            str(chat_id),
        )
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
        until_date = utcnow() + timedelta(seconds=31)

        # 只需设置 can_send_messages=True 即可恢复权限。
        # 在默认模式下（use_independent_chat_permissions=False），
        # can_send_messages=True 会通过群组默认权限隐式继承其他媒体权限
        # （can_send_photos, can_send_videos 等），用户在 31 秒后会从
        # restricted 列表移除，成为普通成员，拥有群组默认的所有权限。
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
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
    chat_title: str | None,
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
        chat_title: 群组标题原始文本（由本函数按 locale 转义并处理无标题回退）
        message_type: 结果类型（success / success_join_request / restore_failed /
            approve_failed）
        group_chat_id: 来源群 ID（必传，用于解析私聊 locale 并保证文案走 catalog）。
    """
    locale = await get_resolver().for_private_from_group(
        user_id=user_id, group_chat_id=group_chat_id
    )
    localizer = get_translator().for_locale(locale)
    safe_title = (
        escape_html(chat_title) if chat_title else localizer.t("common.chat.untitled_group.label")
    )
    text = localizer.t(
        f"verification.success.private.{message_type}.message",
        chat_title=safe_title,
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
    bot: Bot,
    chat_id: int,
    user_id: int,
    username: str,
    mode: str = "join",
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    bio: str | None = None,
) -> bool:
    """检测用户信息是否为垃圾

    名字与 bio 优先取事件自带字段：join_request 的 ChatJoinRequest 事件直接
    携带 bio（权威来源，不再额外获取）；直接入群的 ChatMemberUpdated 无 bio
    字段，缺失时按 Telethon full user → Bot API getChat 顺序补齐——getChat
    仅对曾与 Bot 私聊过的用户返回 bio（tdlib#839），入群场景下大多拿不到，
    故 Telethon 优先；两级都失败则只检测名字（不阻断流程）。

    Args:
        bot: Bot 实例
        chat_id: 群组 ID
        user_id: 用户 ID
        username: 用户名（仅用于日志）
        mode: 处理模式，"join" 或 "join_request"
        first_name: 用户名（事件自带，空则不计入检测文本）
        last_name: 用户姓（事件自带，空则不计入检测文本）
        bio: 用户简介（join_request 事件自带；join 模式留空则由本函数补齐）

    Returns:
        True: 检测到垃圾信息并已处理
        False: 通过检测，继续正常流程
    """
    try:
        if not bio and mode == "join":
            try:
                bio = await get_user_status_service().get_user_bio(chat_id, user_id)
            except Exception as e:
                # 服务层契约上不应抛出；防御契约破坏时降级 getChat 而非放弃检测
                logger.debug(f"Telethon 获取用户 bio 异常 [用户:{user_id}]: {e}")

            if not bio:
                try:
                    user_info = await bot.get_chat(user_id)
                    bio = getattr(user_info, "bio", None)
                except Exception as e:
                    logger.debug(f"getChat 获取用户 bio 失败 [用户:{user_id}]: {e}")

        # 构建检测文本：名字 + bio
        check_texts = []
        if first_name:
            check_texts.append(first_name)
        if last_name:
            check_texts.append(last_name)
        if bio:
            check_texts.append(bio)

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
                    until_date=utcnow() + timedelta(hours=1),
                )
                logger.info(f"已封禁垃圾用户 {user_id} 1 小时")
            except Exception as ban_error:
                logger.error(f"封禁用户失败: {ban_error}")

            return True  # 已检测到垃圾并处理

    except Exception as e:
        logger.error(f"检测用户信息失败: {e}")
        return False  # 检测失败，继续正常流程

    return False  # 通过检测


async def prepare_verification_challenge(
    group,
    chat_id: int,
    user_id: int,
    *,
    locale: str,
) -> PreparedChallenge:
    """按群配置执行纯 prepare；random 在 service 内解析为具体类型，不写正式 Redis 键。"""
    return await VerificationService.prepare_challenge(
        group.verification_type,
        chat_id,
        user_id,
        locale=locale,
    )


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
    chat_title = chat.title

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


def dispatch_verification_timeout(
    bot: Bot,
    chat_id: int,
    user_id: int,
    *,
    flow: str,
    session_id: str,
    timeout: int,
) -> asyncio.Task:
    """按 flow 选择 handler 并派发验证 timeout 任务（live 与启动恢复共用）。

    经 ``spawn_background_task`` 派发：强引用统一由 core.tasks 持有，
    进程关闭时 ``cancel_all_background_tasks`` 一并取消（Redis 状态保留，
    重启后由恢复扫描重新派发）。
    """
    timeout_handler = (
        handle_join_request_timeout if flow == "join_request" else handle_verification_timeout
    )
    return spawn_background_task(
        timeout_handler(bot, chat_id, user_id, session_id=session_id, timeout=timeout)
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
    private_locale = await get_resolver().for_private_from_group(
        user_id=user_id, group_chat_id=chat_id
    )

    reservation = await reserve_initial_recovery(chat_id, user_id, session_id, timeout * 1000)
    if reservation is None:
        # 残留旧会话（inflight 锁正常时不该发生）；交给旧会话 timeout 兜底
        logger.warning(f"初始验证发送权竞争失败 [群组:{chat_id}] [用户:{user_id}]")
        return "busy"

    try:
        prepared = await prepare_verification_challenge(
            group, chat_id, user_id, locale=private_locale
        )
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
        dispatch_verification_timeout(
            bot, chat_id, user_id, flow=flow, session_id=session_id, timeout=timeout
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

    chat_title: str | None = None
    with contextlib.suppress(Exception):
        chat = await bot.get_chat(chat_id)
        chat_title = chat.title

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
    if await check_user_spam_info(
        bot,
        chat_id,
        user_id,
        username,
        mode="join_request",
        first_name=event.from_user.first_name,
        last_name=event.from_user.last_name,
        bio=event.bio,
    ):
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

    chat_title = event.chat.title

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
        # 显式禁用所有权限，包括最新的 can_react_to_messages 和 can_edit_tag，
        # 遵循最小权限原则，确保待验证用户无法进行任何群组交互。
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
                can_react_to_messages=False,  # 禁止添加表情反应
                can_edit_tag=False,  # 禁止编辑用户标签
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
    if await check_user_spam_info(
        bot,
        chat_id,
        user_id,
        username,
        mode="join",
        first_name=user.first_name,
        last_name=user.last_name,
    ):
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
                until_date=utcnow() + timedelta(hours=1),
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

        # 一次 MGET 校验 pending + 类型 + 答案（消除 TOCTOU，防跨类型旧消息重放）
        verification_service = VerificationService()
        answer_result = await verification_service.verify_choice_answer(
            chat_id, user_id, expected_type, answer
        )
        if answer_result.status == "expired":
            await callback.answer(
                localizer.t("verification.callback.expired.toast"), show_alert=False
            )
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            return

        if answer_result.status == "correct":
            is_join_request = answer_result.flow == "join_request"
            await handle_verification_success(
                bot, callback, chat_id, user_id, is_join_request=is_join_request
            )
        else:
            # claim_failure 已原子消费整个 session（recovery/main/deadline/type/token 删除），
            # flow 随 claim 返回，无需再 GET type 或事后 clear（防误删 grace 期后的新 session）
            is_join_request = answer_result.flow == "join_request"

            # 类型差异化答错 toast（honeypot 陷阱单独处理）
            if prefix == "verify_honeypot" and answer == "trap":
                await callback.answer(
                    localizer.t("verification.callback.honeypot_trap.toast"), show_alert=True
                )
                logger.warning(f"用户 {user_id} 触发蜜罐陷阱")
            else:
                await callback.answer(localizer.t(_CHOICE_WRONG_KEYS[prefix]), show_alert=True)

            # 根据 claim 返回的 flow 决定踢出或拒绝
            if is_join_request:
                # 拒绝加入请求并封禁1小时，防止立即重试
                await decline_join_request(bot, chat_id, user_id)
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=utcnow() + timedelta(hours=1),
                )
            else:
                # 踢出并封禁 1 小时
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=utcnow() + timedelta(hours=1),
                )

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

        # 原子校验 deadline/main/recovery 后绑定 session：防 GET→SET 竞态，且 recovery
        # message_id 校验拒绝新 session 建立后点击残留旧验证消息的按钮。
        redis = get_redis()
        waiting_key = RedisKeys.captcha_waiting(chat_id, user_id)
        waiting_user_key = RedisKeys.captcha_waiting_user(user_id)
        waiting_value = await redis.eval(
            _CAPTCHA_WAITING_SET_SCRIPT,
            5,
            RedisKeys.verification_deadline(chat_id, user_id),
            RedisKeys.verification(chat_id, user_id),
            RedisKeys.verification_recovery(chat_id, user_id),
            waiting_key,
            waiting_user_key,
            str(callback.message.message_id),
            str(chat_id),
        )

        if waiting_value:
            await callback.answer(
                localizer.t("verification.captcha.input_prompt.toast"), show_alert=False
            )
        else:
            await callback.answer(
                localizer.t("verification.callback.expired.toast"), show_alert=False
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

        prepared = await verification_service.prepare_challenge(
            "captcha", chat_id, user_id, locale=private_locale
        )
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

        # 检查是否在等待输入状态（state 类型校验由下方 clear_token.state_value 完成）
        waiting_key = RedisKeys.captcha_waiting(chat_id, user_id)
        waiting_value = await redis.get(waiting_key)

        if not waiting_value:
            # 未点击"输入验证码"按钮
            return

        # waiting 值：新格式 {session}:{message_id} 或旧格式 {message_id}（滚动发布残留）
        waiting_session, sep, message_id_str = waiting_value.partition(":")
        if not sep:
            message_id_str = waiting_session  # 旧格式：纯 message_id，无 session
            waiting_session = ""

        # MGET(main, deadline, recovery) 快照：绑定 waiting 与当前 captcha session
        clear_token = await verification_service.capture_clear_token(chat_id, user_id)
        deadline_value = clear_token.deadline_value or ""
        deadline_session = deadline_value.rpartition(":")[0] if ":" in deadline_value else ""

        # 校验 state 是 captcha + waiting session 匹配当前 deadline session
        state_value = clear_token.state_value
        state_is_captcha = state_value is not None and state_value.startswith("captcha:")
        if not state_is_captcha or not deadline_session or not message_id_str.isdigit():
            await _clear_captcha_waiting_if_match(chat_id, user_id, waiting_value)
            return

        if waiting_session:
            # 新格式：session 必须匹配当前 deadline（P2.2 核心：防旧 waiting 用新 session main）
            if waiting_session != deadline_session:
                await _clear_captcha_waiting_if_match(chat_id, user_id, waiting_value)
                return
        else:
            # 旧格式：校验 waiting message_id 仍是当前 session 的 recovery UI（防残留误用）
            recovery_parts = (clear_token.recovery_value or "").split(":")
            if not (
                len(recovery_parts) == 5
                and recovery_parts[1] == deadline_session
                and recovery_parts[4] == message_id_str
            ):
                await _clear_captcha_waiting_if_match(chat_id, user_id, waiting_value)
                return

        # 验证答案（expected_deadline_value 堵校验后到 verify_answer MGET 间的 session 切换；
        # correct/wrong 均在内部原子 claim，与 timeout 互斥）
        answer_result = await verification_service.verify_answer(
            chat_id, user_id, text_input, expected_deadline_value=deadline_value
        )

        if answer_result.status == "expired":
            # timeout 已 claim 或 session 已切换：不恢复权限，也不执行失败处罚
            await _clear_captcha_waiting_if_match(chat_id, user_id, waiting_value)
            return

        if answer_result.status == "correct":
            # claim_success 已清 recovery/main/deadline/type/token；这里清 captcha 输入辅助键
            await _clear_captcha_waiting_if_match(chat_id, user_id, waiting_value)

            is_join_request = answer_result.flow == "join_request"

            # 删除验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=int(message_id_str))

            await message.answer(localizer.t("verification.captcha.input_success.private.message"))

            # ✅ 设置"已验证"标记（10 分钟），以便权限恢复/批准失败后用户重新触发入群时跳过验证
            approved_key = RedisKeys.verification_approved(chat_id, user_id)
            await redis.setex(approved_key, 600, "1")

            # 获取群组标题（用于失败降级文案）
            chat_title: str | None = None
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = chat.title

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
            # claim_failure 已原子消费主状态；waiting 是独立辅助键，仍需按值 CAS 清理
            await message.answer(localizer.t("verification.captcha.input_wrong.private.message"))
            await _clear_captcha_waiting_if_match(chat_id, user_id, waiting_value)

            # 根据 claim 原子返回的 flow 决定踢出或拒绝
            if answer_result.flow == "join_request":
                # 拒绝加入请求
                await decline_join_request(bot, chat_id, user_id)
            else:
                # 踢出并封禁 1 小时
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    until_date=utcnow() + timedelta(hours=1),
                )

                # 删除验证消息
                with contextlib.suppress(Exception):
                    await bot.delete_message(chat_id=user_id, message_id=int(message_id_str))

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
            until_date=utcnow() + timedelta(hours=1),
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

        # WebApp 不走 claim_success。type/main/deadline 使用相同绝对过期时间，且 Telegram
        # 副作用受下面的 CAS clear 成功门控，因此没有成功 claim 后再 GET 的 TTL 窗口。
        # 但 type 独立损坏时必须 fail closed，不能默认走 direct-join 权限恢复。
        type_key = RedisKeys.verification_type(chat_id, user_id)
        verification_type = await redis.get(type_key)
        if verification_type not in ("join", "join_request"):
            logger.info(
                f"忽略 flow 缺失或损坏的 WebApp 回调 "
                f"[provider:{provider}] [user:{user_id}] [chat:{chat_id}]"
            )
            await send_error("verification.webapp.error.expired.private.message")
            return
        is_join_request = verification_type == "join_request"

        # token/type 由 CAS clear 原子消费（不单独 DEL，防删新 session）。timeout 已 claim /
        # 新 session 建立 / 并发重复回调胜出时 clear 失败，必须停止（不恢复权限）。
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
                chat_title = chat.title
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
                    chat_title = chat.title
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
                chat_title = chat.title
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
        chat_title = chat.title  # success helper 统一处理 HTML 转义与无标题回退

        # claim_success 已在 verify_choice_answer 内原子删除整个 session（含 recovery）；
        # 此处不再 clear，避免误删 grace 期后建立的新会话

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
                until_date=utcnow() + timedelta(hours=1),
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
            chat_title = chat.title
            timeout_locale = await get_resolver().for_private_from_group(
                user_id=user_id, group_chat_id=chat_id
            )
            timeout_localizer = get_translator().for_locale(timeout_locale)
            safe_chat_title = (
                escape_html(chat_title)
                if chat_title
                else timeout_localizer.t("common.chat.untitled_group.label")
            )
            timeout_text = timeout_localizer.t(
                "verification.timeout.private.join.message",
                chat_title=safe_chat_title,
                timeout=timeout,
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


async def _build_hint_content(
    bot: Bot,
    chat_id: int,
    flow: VerificationHintFlow,
    mention_ids: Sequence[int] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    """按群 locale 构建 flow 对应的验证引导内容（发送与编辑共用同一套渲染）。

    chat_title 获取失败时回退到语言无关的 chat_id，避免重新引入硬编码中文。
    mention_ids 非空时在正文前加一行匿名 mention（仅 join flow 有此文案）。
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

    text = localizer.t(
        f"verification.hint.{flow}.group.message",
        chat_title=escape_html(chat_title),
    )
    if flow == "join" and mention_ids:
        # anonymous_mentions_html 只由数字 user_id 拼成，是可信 HTML，不能再转义
        mention_line = localizer.t(
            "verification.hint.join.group.mentions",
            users=anonymous_mentions_html(mention_ids),
        )
        text = f"{mention_line}\n\n{text}"

    return text, keyboard


async def _send_hint_message(
    bot: Bot,
    chat_id: int,
    flow: VerificationHintFlow,
    mention_ids: Sequence[int] = (),
) -> Message:
    """发送对应 flow 的验证引导消息（未启动 Bot 提示）。"""
    text, keyboard = await _build_hint_content(bot, chat_id, flow, mention_ids)
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


def _log_mention_overflow(chat_id: int, flow: VerificationHintFlow, shown: int, total: int) -> None:
    """记录 mention 溢出：Telegram 只有前 5 个 mention 触发通知，多渲染无收益。"""
    if total > shown:
        logger.info(
            f"群组 {chat_id} 的 {flow} 引导消息 mention 溢出：{total} 人等待验证，"
            f"仅前 {shown} 人被 mention"
        )


async def _register_hint_user(
    chat_id: int,
    flow: VerificationHintFlow,
    user_id: int,
    ttl: int,
) -> tuple[bool, bool, int]:
    """登记待 mention 用户；Redis 故障时降级为「本次不 mention」而非中断引导。"""
    try:
        return await add_hint_user(chat_id, flow, user_id, ttl=ttl)
    except Exception as e:
        logger.warning(f"登记群组 {chat_id} 的待验证用户 {user_id} 失败，本次不 mention: {e}")
        return False, False, 0


async def _collect_hint_mentions(chat_id: int, flow: VerificationHintFlow) -> list[int]:
    """读取窗口内待 mention 用户（按加入顺序截断到上限）；失败降级为不 mention。"""
    limit = settings.verification_hint_max_mentions
    if limit <= 0:
        return []

    try:
        mention_ids, total = await snapshot_hint_users(chat_id, flow, limit)
    except Exception as e:
        logger.warning(f"读取群组 {chat_id} 的 {flow} 引导 mention 名单失败: {e}")
        return []

    _log_mention_overflow(chat_id, flow, len(mention_ids), total)
    return mention_ids


async def _refresh_hint_mentions(
    bot: Bot,
    chat_id: int,
    flow: VerificationHintFlow,
) -> None:
    """把窗口内新登记的用户补进已发出的引导消息。

    Telegram 不会为「编辑时新增的 mention」推送提醒，故这里只是视觉补全：让晚到
    用户在消息里可见并拿到 @ 徽章。任何失败（消息已删、内容未变化、限流）都只
    降级为消息少一个 👤，绝不影响验证主流程。

    Note:
        claim 与 edit 之间仍有一段网络窗口：两个并发编辑若在此乱序到达，较旧的
        内容可能后落地，导致消息少显示一个 mention。因窗口仅 30 秒、后果纯视觉，
        这里不引入分布式编辑锁；下一个用户加入时版本继续增长会自然补齐。
    """
    limit = settings.verification_hint_max_mentions
    if limit <= 0:
        return

    try:
        claim = await claim_hint_edit(chat_id, flow, limit, ttl=_HINT_SHARE_WINDOW_SECONDS)
    except Exception as e:
        logger.warning(f"取得群组 {chat_id} 的 {flow} 引导 mention 编辑权失败: {e}")
        return

    if claim is None:
        return
    _log_mention_overflow(chat_id, flow, len(claim.mention_ids), claim.total)

    try:
        text, keyboard = await _build_hint_content(bot, chat_id, flow, claim.mention_ids)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=claim.message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        logger.debug(f"群组 {chat_id} 的 {flow} 引导消息已补全 {len(claim.mention_ids)} 个 mention")
    except Exception as e:
        logger.debug(f"补全群组 {chat_id} 的 {flow} 引导消息 mention 失败: {e}")


async def _join_shared_hint(
    bot: Bot,
    chat_id: int,
    flow: VerificationHintFlow,
    mention_user_id: int | None,
) -> None:
    """未取得发送权：登记到当前窗口、延长共享时间，必要时补全 mention。"""
    added = False
    committed = False
    if mention_user_id is not None:
        added, committed, _ = await _register_hint_user(
            chat_id, flow, mention_user_id, _HINT_SHARE_WINDOW_SECONDS
        )

    # 已有 hint（已提交或他人 pending）：已提交则延长共享窗口；pending 不续命。
    # Redis 故障只影响共享时长，不能冒泡到入群流程（否则用户会被误封禁）
    extended = False
    try:
        extended = await try_extend_hint(chat_id, flow)
    except Exception as e:
        logger.warning(f"延长群组 {chat_id} 的 {flow} 引导消息共享窗口失败: {e}")
    if extended:
        logger.debug(
            f"群组 {chat_id} 已有 {flow} 引导消息，延长 TTL 到 {_HINT_SHARE_WINDOW_SECONDS} 秒"
        )

    # 仅「新登记 + 消息已发出」才需要编辑：pending 期间登记的用户会被 owner 的快照
    # 一并发出；是否溢出上限由 claim_hint_edit 在 Redis 侧原子判定
    if added and committed:
        await _refresh_hint_mentions(bot, chat_id, flow)


async def _publish_shared_hint(
    bot: Bot,
    chat_id: int,
    flow: VerificationHintFlow,
    user_id: int | None = None,
) -> None:
    """竞争发送权并发布共享引导消息（NX reservation → send → promote CAS）。

    取不到发送权时尝试延长已提交消息的共享窗口。发送失败或 reservation 过期时
    清理自己的 reservation / 未提交消息，避免第二条 hint 残留。

    传入 user_id 时（join flow）额外把窗口内等待验证的用户匿名 mention 进消息：
    取得发送权者先聚合等待一小段时间，让同批入群的用户进入同一条消息——只有随
    消息一起发出的 mention 才会触发 Telegram 推送提醒；晚到用户改由编辑补全。
    join_request 用户尚未入群、收不到群消息，故不传 user_id、行为保持不变。
    """
    mention_user_id = user_id if settings.verification_hint_max_mentions > 0 else None
    aggregation_delay = (
        settings.verification_hint_aggregation_delay if mention_user_id is not None else 0.0
    )
    # 聚合等待同样消耗窗口 TTL（promote 用 KEEPTTL），据此加长预留，
    # 使消息发出后仍有完整的共享窗口
    reserve_ttl = _HINT_SHARE_WINDOW_SECONDS + math.ceil(aggregation_delay)

    try:
        owner_token = await reserve_hint(chat_id, flow, ttl=reserve_ttl)
    except Exception as e:
        # 引导消息只是尽力提醒：Redis 故障时跳过即可，challenge 已标记 undelivered，
        # 由同 session 的 timeout task 兜底。异常若冒泡到 on_user_join 会走封禁分支，
        # 把 Redis 抖动变成对正常新用户的误封
        logger.warning(f"群组 {chat_id} 竞争 {flow} 引导发送权失败，跳过引导消息: {e}")
        return

    if owner_token is None:
        await _join_shared_hint(bot, chat_id, flow, mention_user_id)
        return

    hint_msg: Message | None = None
    try:
        mention_ids: list[int] = []
        if mention_user_id is not None:
            await _register_hint_user(chat_id, flow, mention_user_id, reserve_ttl)
            if aggregation_delay > 0:
                await asyncio.sleep(aggregation_delay)
            mention_ids = await _collect_hint_mentions(chat_id, flow)

        hint_msg = await _send_hint_message(bot, chat_id, flow, mention_ids)
        if await promote_hint(chat_id, flow, owner_token, hint_msg.message_id):
            if mention_ids:
                with contextlib.suppress(Exception):
                    await claim_hint_render(
                        chat_id,
                        flow,
                        hint_msg.message_id,
                        len(mention_ids),
                        ttl=_HINT_SHARE_WINDOW_SECONDS,
                    )
            asyncio.create_task(
                delete_hint_message_after_delay(
                    bot, chat_id, hint_msg.message_id, flow, _HINT_SHARE_WINDOW_SECONDS
                )
            )
            logger.info(
                f"群组 {chat_id} 发送 {flow} 验证引导消息"
                f"（{_HINT_SHARE_WINDOW_SECONDS}秒内共享，mention {len(mention_ids)} 人）"
            )
            if mention_user_id is not None:
                # 补检：覆盖「快照之后、promote 之前」挤进窗口的用户
                await _refresh_hint_mentions(bot, chat_id, flow)
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
        if hint_msg is not None:
            # promote 抛错（如 Redis 故障）时消息已发出却未纳入状态机：没有删除任务
            # 会回收它，此处补删，避免群里永久残留一条引导消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=hint_msg.message_id)
        logger.error(f"发送 {flow} 引导消息失败", exc_info=True)


async def handle_user_not_started_bot(bot: Bot, chat_id: int, user_id: int) -> None:
    """直接入群用户未启动 Bot：发布共享引导消息。

    challenge 已由 _start_initial_verification 标记为 undelivered，并由同 session 的
    timeout task 兜底处罚，故此处不再另启 timeout。30 秒内同一 flow 只发一条引导消息，
    消息内以匿名 mention 提醒窗口内所有等待验证的用户（用户已入群，mention 必定生效）。
    """
    await _publish_shared_hint(bot, chat_id, "join", user_id)
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
                until_date=utcnow() + timedelta(hours=1),
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
            chat_title = chat.title
            timeout_locale = await get_resolver().for_private_from_group(
                user_id=user_id, group_chat_id=chat_id
            )
            timeout_localizer = get_translator().for_locale(timeout_locale)
            safe_chat_title = (
                escape_html(chat_title)
                if chat_title
                else timeout_localizer.t("common.chat.untitled_group.label")
            )
            timeout_text = timeout_localizer.t(
                "verification.timeout.private.join_request.message",
                chat_title=safe_chat_title,
                timeout=timeout,
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


# 启动恢复扫描的批大小：每批两次 MGET（deadline 值 + 有效会话的 verification_type），
# 替代逐键 2 次 GET——500 个会话从 1000 次串行 RTT 降为约 10 次往返
_RESUME_SCAN_BATCH_SIZE = 100


def _parse_deadline_entry(key: str, raw_value: object) -> tuple[int, int, str] | None:
    """解析单个 deadline 键名与值，返回 (chat_id, user_id, session_id)；脏数据返回 None。

    值格式的权威解析在 :func:`parse_deadline_value`（verification_recovery），
    此处只负责键名三段拆分与其对接，防止多处手写校验漂移。
    """
    parts = key.split(":")
    if len(parts) != 3:
        logger.debug(f"跳过格式错误的验证 deadline 键: {key}")
        return None
    try:
        chat_id, user_id = int(parts[1]), int(parts[2])
    except ValueError:
        logger.debug(f"跳过无法解析 ID 的验证 deadline 键: {key}")
        return None

    raw = redis_text(raw_value)
    if raw is None:
        # 键在 SCAN 与 MGET 之间已自然过期或被成功路径清理
        return None

    parsed = parse_deadline_value(raw)
    if parsed is None:
        logger.debug(f"跳过格式损坏的验证 deadline 值 [群组:{chat_id}] [用户:{user_id}]")
        return None
    return chat_id, user_id, parsed[0]


async def _resume_deadline_batch(
    bot: Bot,
    redis: Redis,
    group_repo: GroupRepository,
    timeout_cache: dict[int, int],
    keys: list[str],
) -> tuple[int, list[str]]:
    """解析并派发一批 deadline 键（每批两次 MGET），返回 (派发数, 失败键列表)。

    脏数据（键名/值/flow 非法或键已消失）静默跳过；单会话依赖故障（如群配置
    读取失败）只失败该会话，键计入失败列表待补扫；Redis MGET 级故障抛给
    调用方按整批失败处理。
    """
    resumed = 0
    failed_keys: list[str] = []

    # 第一轮 MGET：deadline 值
    raw_values = await redis.mget(keys)
    entries: list[tuple[str, int, int, str]] = []  # (key, chat_id, user_id, session_id)
    for key, raw_value in zip(keys, raw_values, strict=True):
        parsed = _parse_deadline_entry(key, raw_value)
        if parsed is not None:
            entries.append((key, *parsed))

    # 第二轮 MGET：verification_type
    flows = await redis.mget(
        [RedisKeys.verification_type(chat_id, user_id) for _, chat_id, user_id, _ in entries]
    )
    for (key, chat_id, user_id, session_id), raw_flow in zip(entries, flows, strict=True):
        flow = redis_text(raw_flow)
        if flow not in ("join", "join_request"):
            # fail-safe：宁可不恢复也不错罚
            logger.warning(
                f"跳过 flow 缺失或非法的验证会话 [群组:{chat_id}] [用户:{user_id}] [flow:{flow!r}]"
            )
            continue

        # timeout 仅用于日志与私聊文案插值（deadline 判断在 Redis），按群组配置
        # 取值保持文案与原会话一致；扫描内缓存避免同群 N+1 查询
        try:
            if chat_id not in timeout_cache:
                group_config = await group_repo.get(chat_id)
                timeout_cache[chat_id] = (
                    group_config.verification_timeout
                    if group_config
                    else settings.verification_timeout
                )
        except Exception as e:
            logger.warning(f"读取验证超时配置失败，待补扫 [键:{key}] [异常:{e}]")
            failed_keys.append(key)
            continue

        dispatch_verification_timeout(
            bot,
            chat_id,
            user_id,
            flow=flow,
            session_id=session_id,
            timeout=timeout_cache[chat_id],
        )
        resumed += 1

    return resumed, failed_keys


async def resume_pending_verification_timeouts(bot: Bot) -> int:
    """启动时恢复 Redis 中仍在进行的验证会话 timeout 任务。

    进程重启会丢失原 timeout 任务（内存态），deadline 已过的会话将无人处罚
    （deadline+grace 后 Redis 键过期，沦为僵尸会话）。本函数扫描
    ``verification_deadline:*``（模式经 ``RedisKeys.verification_deadline_pattern``），
    按批 MGET（deadline 值 + verification_type）后为每个会话重新派发对应 flow
    的 timeout 协程——经 ``dispatch_verification_timeout`` 统一派发，强引用与
    关闭取消由 core.tasks 管理；立即处罚、继续等待还是退出仍由 ``claim_timeout``
    的 Redis Lua 状态机决定，与 success、/start 恢复、事件补投天然互斥。

    Returns:
        成功派发的 timeout 协程数（不代表处罚已完成）
    """
    resumed = 0
    failed_keys: list[str] = []
    timeout_cache: dict[int, int] = {}

    try:
        redis = get_redis()
        group_repo = GroupRepository()

        async def _flush(pending: list[str]) -> None:
            """处理一批键：批级异常（如 Redis 故障）整批计失败待补扫"""
            nonlocal resumed
            if not pending:
                return
            try:
                count, failed = await _resume_deadline_batch(
                    bot, redis, group_repo, timeout_cache, pending
                )
                resumed += count
                failed_keys.extend(failed)
            except Exception as e:
                logger.warning(f"恢复验证 timeout 批次失败，整批待补扫 [异常:{e}]")
                failed_keys.extend(pending)

        seen_keys: set[str] = set()
        batch: list[str] = []
        try:
            async for raw_key in redis.scan_iter(
                match=RedisKeys.verification_deadline_pattern(), count=100
            ):
                key = redis_text(raw_key)
                if key is None:
                    continue
                # SCAN 在 rehash 等情况下可能返回重复键
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                batch.append(key)
                if len(batch) >= _RESUME_SCAN_BATCH_SIZE:
                    await _flush(batch)
                    batch = []
        except Exception as e:
            # SCAN 中途故障（游标抖动等）：已收集的键不能随异常丢弃，
            # 继续处理残余批——未扫描到的部分只能留待键自然过期
            logger.warning(f"SCAN 中途故障，继续处理已收集的 {len(batch)} 个键: {e}")
        await _flush(batch)

        # 补扫一轮依赖瞬时故障的键（先摘下再清空，避免 _flush 向自身追加）
        retry_keys, failed_keys = failed_keys, []
        await _flush(retry_keys)
        if failed_keys:
            # 补扫仍失败：接受丢失（Redis 键自然过期），不阻断启动
            logger.warning(f"补扫恢复验证 timeout 仍失败，放弃 {len(failed_keys)} 个会话")

    except Exception as e:
        # 扫描级失败不影响 Bot 启动
        logger.exception(f"启动时扫描验证 timeout 失败: {e}")

    logger.info(f"启动验证 timeout 恢复完成：已派发 {resumed} 个会话")
    return resumed


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

    private_locale = await get_resolver().for_private_from_group(
        user_id=user_id, group_chat_id=chat_id
    )
    localizer = get_translator().for_locale(private_locale)
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
            await _recover_verification_challenge(
                message, bot, chat_id, user_id, flow, private_locale, _answer
            )
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
        await _recover_verification_challenge(
            message, bot, chat_id, user_id, flow, private_locale, _answer
        )
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
    private_locale: str,
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
        prepared = await verification_service.prepare_challenge(
            challenge_type, chat_id, user_id, locale=private_locale
        )
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
