"""入群验证处理器"""

import asyncio
import contextlib
from datetime import datetime, timedelta

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

from src.core.cache import PermissionCache
from src.core.redis import RedisKeys, get_redis
from src.core.utils import escape_html, format_user_mention
from src.repositories.group_repo import GroupRepository
from src.services.spam_detector import SpamDetector
from src.services.username_mapping import UsernameMappingService
from src.services.verification import VerificationService

router = Router(name="verification")


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


async def restore_user_permissions(bot: Bot, chat_id: int, user_id: int) -> bool:
    """恢复用户权限并从 restricted 列表移除（30秒后自动移除）

    修复 unban_chat_member(..., only_if_banned=False) 导致 restricted 用户被踢出的 bug

    Args:
        bot: Bot 实例
        chat_id: 群组 ID
        user_id: 用户 ID

    Returns:
        是否成功
    """
    try:
        # 获取用户当前状态
        member = await bot.get_chat_member(chat_id, user_id)

        if member.status == "kicked":
            # 用户被封禁，解除封禁
            await bot.unban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                only_if_banned=True,
            )
        elif member.status == "restricted":
            # 用户被禁言，恢复权限 + 31秒后自动移出 restricted 列表
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
        return True

    except Exception as e:
        logger.error(f"恢复用户权限失败 [用户:{user_id}]: {e}")
        return False


async def send_verification_success_message(
    bot: Bot,
    user_id: int,
    chat_id: int,
    chat_title: str,
    message_type: str = "success",
) -> None:
    """发送验证成功消息（带一次性邀请链接）

    Args:
        bot: Bot 实例
        user_id: 用户 ID
        chat_id: 群组 ID
        chat_title: 群组标题（已转义 HTML）
        message_type: 消息类型
            - "success": 验证成功，已自动加入
            - "success_join_request": 验证成功，加入请求已批准
            - "failed_restore": 验证成功，但恢复权限失败
    """
    # 创建一次性邀请链接（10分钟有效）
    invite_link = None
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=chat_id,
            expire_date=datetime.now() + timedelta(minutes=10),
            member_limit=1,  # 一次性链接
            creates_join_request=False,  # 直接加入，不需要批准
        )
        invite_link = invite.invite_link
        logger.info(f"已为用户 {user_id} 创建一次性邀请链接（{message_type}）")
    except Exception as e:
        logger.warning(f"创建邀请链接失败: {e}")

    # 根据消息类型构建文本
    if message_type == "success":
        text = f"✅ <b>验证成功！</b>\n\n您已成功加入群组：<b>{chat_title}</b>\n\n现在可以在群内自由发言了！"
    elif message_type == "success_join_request":
        text = f"✅ <b>验证成功！</b>\n\n您的加入请求已批准，正在加入群组：<b>{chat_title}</b>\n\n稍后您将能在群内自由发言！"
    elif message_type == "failed_restore":
        text = f"✅ <b>验证成功！</b>\n\n由于网络问题，无法自动恢复您的权限。\n\n请点击下方按钮加入群组：<b>{chat_title}</b>\n\n您已通过验证，将自动获得权限！"
    else:
        text = f"✅ <b>验证成功！</b>\n\n群组：<b>{chat_title}</b>"

    # 添加邀请链接提示和按钮
    if invite_link:
        if message_type in ["success", "success_join_request"]:
            text += "\n\n💡 如果没有自动加入，请点击下方按钮手动加入："
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 点击加入群组", url=invite_link)]]
        )
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
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
        result = await detector.detect(
            text=check_text,
            user_id=user_id,
            chat_id=chat_id,
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


async def generate_verification_challenge(group, chat_id: int, user_id: int, username: str):
    """根据群组配置生成验证挑战

    Args:
        group: 群组配置对象
        chat_id: 群组 ID
        user_id: 用户 ID
        username: 用户名

    Returns:
        验证挑战对象
    """
    verification_service = VerificationService()

    if group.verification_type == "math":
        return await verification_service.generate_math_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "slider":
        return await verification_service.generate_slider_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "qa":
        return await verification_service.generate_qa_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "emoji":
        return await verification_service.generate_emoji_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "captcha":
        return await verification_service.generate_captcha_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "honeypot":
        return await verification_service.generate_honeypot_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "puzzle":
        return await verification_service.generate_puzzle_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "turnstile":
        return await verification_service.generate_turnstile_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "friendly":
        return await verification_service.generate_friendly_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "hcaptcha":
        return await verification_service.generate_hcaptcha_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "mtcaptcha":
        return await verification_service.generate_mtcaptcha_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "altcha":
        return await verification_service.generate_altcha_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    elif group.verification_type == "random":
        return await verification_service.generate_random_challenge(
            chat_id, user_id, username, group.verification_timeout
        )
    else:  # 默认数学验证
        return await verification_service.generate_math_challenge(
            chat_id, user_id, username, group.verification_timeout
        )


async def send_verification_message(
    bot: Bot, chat_id: int, user_id: int, challenge, message_title: str, message_prefix: str
):
    """发送验证消息到用户私聊

    Args:
        bot: Bot 实例
        chat_id: 群组 ID
        user_id: 用户 ID
        challenge: 验证挑战对象
        message_title: 消息标题（如 "加入请求验证" 或 "群组验证通知"）
        message_prefix: 消息前缀（如 "您请求加入群组" 或 "您加入了群组"）

    Returns:
        发送的消息对象
    """
    # 获取群组信息
    chat = await bot.get_chat(chat_id)
    chat_title = escape_html(chat.title) if chat.title else "群组"

    # 构造消息文本（使用 HTML 格式以防注入）
    message_text = f"📢 <b>{message_title}</b>\n\n{message_prefix}：<b>{chat_title}</b>\n\n{challenge.question}"

    # 根据是否有图片选择发送方式
    if challenge.photo:
        # captcha 验证：发送图片
        return await bot.send_photo(
            chat_id=user_id,
            photo=challenge.photo,
            caption=message_text,
            reply_markup=challenge.keyboard,
            parse_mode="HTML",
        )
    else:
        # 其他验证：发送文本消息
        return await bot.send_message(
            chat_id=user_id,
            text=message_text,
            reply_markup=challenge.keyboard,
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

    # ✅ 新增：记录 username 映射
    if user.username:
        from src.services.username_mapping import UsernameMappingService

        await UsernameMappingService.update_mapping(
            user_id=user_id,
            username=user.username,
        )

    # ==================== 用户信息反垃圾检测 ====================
    if await check_user_spam_info(bot, chat_id, user_id, username, mode="join_request"):
        return  # 检测到垃圾信息，已处理，直接返回
    # ==================== 用户信息反垃圾检测结束 ====================

    try:
        # ✅ 检查用户是否已通过验证（例如之前验证成功但批准失败）
        redis = get_redis()
        approved_key = RedisKeys.verification_approved(chat_id, user_id)
        is_approved = await redis.get(approved_key)

        if is_approved:
            # 用户已通过验证，直接批准加入请求
            logger.info(f"用户 {user_id} 已通过验证，直接批准加入请求")

            try:
                await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
                logger.info(f"已批准用户 {user_id} 的加入请求（已验证用户）")

                # 清除验证标记
                await redis.delete(approved_key)

                # 在私聊中通知用户
                with contextlib.suppress(Exception):
                    chat = await bot.get_chat(chat_id)
                    chat_title = escape_html(chat.title) if chat.title else "群组"
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"✅ <b>加入成功！</b>\n\n您的加入请求已批准，欢迎加入群组：<b>{chat_title}</b>\n\n现在可以在群内自由发言了！",
                        parse_mode="HTML",
                    )

                return  # 已处理，直接返回

            except Exception as e:
                logger.error(f"批准已验证用户的加入请求失败: {e}")
                # 批准失败，继续走正常验证流程

        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id, event.chat.title)

        # ✅ 防止重复验证请求：检查是否已有进行中的验证
        verification_service = VerificationService()
        if await verification_service.is_verification_pending(chat_id, user_id):
            logger.debug(
                f"用户 {user_id} 在群组 {chat_id} 已有进行中的验证（加入请求模式），忽略重复请求"
            )
            return

        # 根据验证类型生成挑战
        challenge = await generate_verification_challenge(group, chat_id, user_id, username)

        # 尝试私聊发送验证消息
        try:
            # 标记验证类型为加入请求验证
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            await redis.setex(type_key, group.verification_timeout + 10, "join_request")

            # 发送验证消息
            sent_message = await send_verification_message(
                bot, chat_id, user_id, challenge, "加入请求验证", "您请求加入群组"
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
            verification_service = VerificationService()
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

    logger.info(f"用户 {username} ({user_id}) 加入群组 {chat_id}")

    # ✅ 记录 username 映射
    if user.username:
        await UsernameMappingService.update_mapping(
            user_id=user_id,
            username=user.username,
        )

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

            # 直接发送欢迎消息（不需要限制权限）
            welcome_msg = await bot.send_message(
                chat_id=chat_id,
                text=f"✅ 欢迎 {format_user_mention(user)} 加入群组！\n\n"
                f"由管理员 {format_user_mention(event.from_user)} 邀请加入。",
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

    # ==================== 用户信息反垃圾检测 ====================
    if await check_user_spam_info(bot, chat_id, user_id, username, mode="join"):
        return  # 检测到垃圾信息，已处理，直接返回
    # ==================== 用户信息反垃圾检测结束 ====================

    try:
        # 检查用户是否已通过加入请求验证
        redis = get_redis()
        approved_key = RedisKeys.verification_approved(chat_id, user_id)
        is_approved = await redis.get(approved_key)

        if is_approved:
            # 用户已通过验证，恢复权限
            logger.info(f"用户 {user_id} 已通过加入请求验证，跳过重复验证")

            # ✅ 恢复用户权限（修复 bug：不再踢出用户）
            await restore_user_permissions(bot, chat_id, user_id)

            # 清除验证标记
            await redis.delete(approved_key)

            # 获取群组信息
            chat = await bot.get_chat(chat_id)
            chat_title = escape_html(chat.title) if chat.title else "群组"  # ✅ 安全修复：转义 HTML

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ <b>验证成功！</b>\n\n您已成功加入群组：<b>{chat_title}</b>\n\n现在可以在群内自由发言了！",
                    parse_mode="HTML",  # ✅ 安全修复：使用 HTML 代替 Markdown
                )

            # 在群内发送欢迎消息
            welcome_msg = await bot.send_message(
                chat_id=chat_id,
                text=f"✅ 欢迎 {format_user_mention(user)} 加入群组！",
            )

            # 5秒后删除欢迎消息
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

            return

        # 用户未通过加入请求验证（未启用 Approve New Members），执行正常验证流程
        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id, event.chat.title)

        # ✅ 防止重复验证请求：检查是否已有进行中的验证
        verification_service = VerificationService()
        if await verification_service.is_verification_pending(chat_id, user_id):
            logger.debug(
                f"用户 {user_id} 在群组 {chat_id} 已有进行中的验证（直接加入模式），忽略重复请求"
            )
            return

        # 根据验证类型生成挑战
        challenge = await generate_verification_challenge(group, chat_id, user_id, username)

        # ✅ 尝试私聊发送验证消息
        try:
            # 发送验证消息
            sent_message = await send_verification_message(
                bot, chat_id, user_id, challenge, "群组验证通知", "您加入了群组"
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


@router.callback_query(F.data.startswith("verify_math:"))
async def on_math_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理数学验证 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str, answer = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # ✅ 检查验证状态是否还存在
        verification_service = VerificationService()
        if not await verification_service.is_verification_pending(chat_id, user_id):
            # 验证状态不存在，可能是：
            # 1. 用户已验证通过（点击了其他验证消息）
            # 2. 用户已超时被踢
            # 3. 用户点击了过期的验证消息
            await callback.answer("✅ 此验证消息已失效", show_alert=False)
            # 尝试删除过期的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            return

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, answer):
            # 检查验证类型
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            await handle_verification_success(bot, callback, chat_id, user_id, is_join_request)
        else:
            await callback.answer("❌ 答案错误", show_alert=True)
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
        logger.error(f"处理数学验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_slider:"))
async def on_slider_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理滑块验证 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str, position = callback.data.split(":")
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

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, position):
            # 检查验证类型
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            await handle_verification_success(bot, callback, chat_id, user_id, is_join_request)
        else:
            await callback.answer("❌ 选择错误", show_alert=True)
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
        logger.error(f"处理滑块验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_qa:"))
async def on_qa_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理问答验证 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str, answer = callback.data.split(":")
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

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, answer):
            # 检查验证类型
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            await handle_verification_success(bot, callback, chat_id, user_id, is_join_request)
        else:
            await callback.answer("❌ 答案错误", show_alert=True)
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
            logger.info(f"用户 {user_id} 问答验证失败")

    except Exception as e:
        logger.error(f"处理问答验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_emoji:"))
async def on_emoji_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理表情验证 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str, answer = callback.data.split(":")
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

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, answer):
            # 检查验证类型
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            await handle_verification_success(bot, callback, chat_id, user_id, is_join_request)
        else:
            await callback.answer("❌ 选择错误", show_alert=True)
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
            logger.info(f"用户 {user_id} 表情验证失败")

    except Exception as e:
        logger.error(f"处理表情验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_honeypot:"))
async def on_honeypot_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理蜜罐验证 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str, answer = callback.data.split(":")
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

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, answer):
            # 检查验证类型
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            await handle_verification_success(bot, callback, chat_id, user_id, is_join_request)
        else:
            # 蜜罐陷阱或答案错误
            if answer == "trap":
                await callback.answer("❌ 检测到机器人行为", show_alert=True)
                logger.warning(f"用户 {user_id} 触发蜜罐陷阱")
            else:
                await callback.answer("❌ 答案错误", show_alert=True)

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
            logger.info(f"用户 {user_id} 蜜罐验证失败")

    except Exception as e:
        logger.error(f"处理蜜罐验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_puzzle:"))
async def on_puzzle_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理拼图验证 - 私聊模式"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 验证数据错误", show_alert=True)
            return

        _, chat_id_str, user_id_str, position = callback.data.split(":")
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

        # 验证答案
        if await verification_service.verify_answer(chat_id, user_id, position):
            # 检查验证类型
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            verification_type = await redis.get(type_key)
            is_join_request = verification_type == "join_request"

            # 清除类型标记
            await redis.delete(type_key)

            await handle_verification_success(bot, callback, chat_id, user_id, is_join_request)
        else:
            await callback.answer("❌ 选择错误", show_alert=True)
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
            logger.info(f"用户 {user_id} 拼图验证失败")

    except Exception as e:
        logger.error(f"处理拼图验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_captcha_input:"))
async def on_captcha_input_request(callback: CallbackQuery, _bot: Bot) -> None:
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

        challenge = await verification_service.generate_captcha_challenge(
            chat_id, user_id, username, timeout
        )

        # 检查 photo 是否存在
        if not challenge.photo:
            await callback.answer("❌ 生成验证码失败", show_alert=True)
            return

        # 编辑消息，更新图片和按钮
        await bot.edit_message_media(
            chat_id=user_id,
            message_id=callback.message.message_id,
            media=InputMediaPhoto(media=challenge.photo),
        )
        await bot.edit_message_caption(
            chat_id=user_id,
            message_id=callback.message.message_id,
            caption=challenge.question,
            reply_markup=(
                challenge.keyboard if isinstance(challenge.keyboard, InlineKeyboardMarkup) else None
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

            await message.answer("✅ 验证成功！")

            # 处理验证成功逻辑
            if is_join_request:
                # 批准加入请求
                await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
                logger.info(f"用户 {user_id} 验证成功，已批准加入请求")
            else:
                # ✅ 恢复用户权限（修复 bug：不再踢出用户）
                await restore_user_permissions(bot, chat_id, user_id)

                # 发送欢迎消息到群组
                assert message.from_user  # 类型缩小
                welcome_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ 欢迎 {message.from_user.mention_html()} 加入群组！",
                    parse_mode="HTML",
                )

                # 30 秒后删除欢迎消息
                async def delayed_delete():
                    await asyncio.sleep(30)
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

                asyncio.create_task(delayed_delete())

                logger.info(f"用户 {user_id} 验证成功")

            # 清除验证状态
            await verification_service.clear_verification(chat_id, user_id)

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

            try:
                await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
                logger.info(
                    f"用户 {user_id} {provider.upper()} 验证成功，已批准加入请求 (群组 {chat_id})"
                )
            except Exception as e:
                logger.error(f"批准加入请求失败: {e}")
                # 即使失败，用户已验证，approved_key 会在用户重新加入时生效

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"
                await send_verification_success_message(
                    bot, user_id, chat_id, chat_title, "success_join_request"
                )

        else:
            # 正常入群模式：恢复群组权限

            # ✅ 设置"已验证"标记（10分钟有效），防止恢复权限失败后用户重新加入被要求验证
            approved_key = RedisKeys.verification_approved(chat_id, user_id)
            await redis.setex(approved_key, 600, "1")  # 10分钟

            # ✅ 恢复用户权限（修复 bug：不再踢出用户）
            success = await restore_user_permissions(bot, chat_id, user_id)

            if not success:
                # 恢复权限失败，通知用户需要重新加入，并提供邀请链接
                with contextlib.suppress(Exception):
                    chat = await bot.get_chat(chat_id)
                    chat_title = escape_html(chat.title) if chat.title else "群组"
                    await send_verification_success_message(
                        bot, user_id, chat_id, chat_title, "failed_restore"
                    )
                return  # 提前返回，不发送欢迎消息

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"
                await send_verification_success_message(
                    bot, user_id, chat_id, chat_title, "success"
                )

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
    """
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

            # 批准加入请求
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await send_verification_success_message(
                    bot, user_id, chat_id, chat_title, "success_join_request"
                )

            await callback.answer("✅ 验证成功！")
            logger.info(f"用户 {user_id} 加入请求验证成功，已批准加入群组 {chat_id}")

        else:
            # 正常入群模式：恢复群组权限
            # ✅ 恢复用户权限（修复 bug：不再踢出用户）
            await restore_user_permissions(bot, chat_id, user_id)

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await send_verification_success_message(
                    bot, user_id, chat_id, chat_title, "success"
                )

            # 在群内发送欢迎消息（仅此一条群内消息）
            welcome_msg = await bot.send_message(
                chat_id=chat_id,
                text=f"✅ 欢迎 {format_user_mention(callback.from_user)} 加入群组！",
            )

            # 5秒后删除欢迎消息
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

            await callback.answer("✅ 验证成功！")
            logger.info(f"用户 {user_id} 私聊验证成功，已加入群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理验证成功失败: {e}")
        with contextlib.suppress(Exception):
            await callback.answer("❌ 处理失败，请联系管理员", show_alert=True)


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
                await bot.send_message(
                    chat_id=user_id,
                    text=f"❌ <b>验证超时</b>\n\n您在群组 <b>{chat_title}</b> 的验证已超时（超过 {timeout} 秒）。请重新加入并在规定时间内完成验证。",
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
                await bot.send_message(
                    chat_id=user_id,
                    text=f"❌ <b>验证超时</b>\n\n您加入群组 <b>{chat_title}</b> 的请求已被拒绝，原因：验证超时（超过 {timeout} 秒）。请重新发送加入请求并在规定时间内完成验证。",
                    parse_mode="HTML",
                )

            # 5. 清除验证状态
            await verification_service.clear_verification(chat_id, user_id)

            # 6. 清除验证类型标记
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            await redis.delete(type_key)

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

    # 7. 清除验证类型标记
    type_key = RedisKeys.verification_type(chat_id, user_id)
    await redis.delete(type_key)
