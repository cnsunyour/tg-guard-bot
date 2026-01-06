"""入群验证处理器"""

import asyncio
import contextlib
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from loguru import logger

from src.core.database import get_db_session
from src.core.redis import RedisKeys, get_redis
from src.core.utils import format_user_mention
from src.repositories.group_repo import GroupRepository
from src.services.verification import VerificationService

router = Router(name="verification")


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

    try:
        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id, event.chat.title)

        # 根据验证类型生成挑战
        verification_service = VerificationService()

        if group.verification_type == "math":
            challenge = await verification_service.generate_math_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "slider":
            challenge = await verification_service.generate_slider_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "qa":
            challenge = await verification_service.generate_qa_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "emoji":
            challenge = await verification_service.generate_emoji_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "captcha":
            challenge = await verification_service.generate_captcha_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "honeypot":
            challenge = await verification_service.generate_honeypot_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "random":
            challenge = await verification_service.generate_random_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        else:  # 默认数学验证
            challenge = await verification_service.generate_math_challenge(
                chat_id, user_id, username, group.verification_timeout
            )

        # 尝试私聊发送验证消息
        try:
            # 获取群组信息
            chat = await bot.get_chat(chat_id)
            chat_title = chat.title or "群组"

            # 标记验证类型为加入请求验证
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            await redis.setex(type_key, group.verification_timeout + 10, "join_request")

            # 根据是否有图片选择发送方式
            if challenge.photo:
                # captcha 验证：发送图片
                sent_message = await bot.send_photo(
                    chat_id=user_id,
                    photo=challenge.photo,
                    caption=f"📢 **加入请求验证**\n\n您请求加入群组：**{chat_title}**\n\n{challenge.question}",
                    reply_markup=challenge.keyboard,
                    parse_mode="Markdown",
                )
            else:
                # 其他验证：发送文本消息
                sent_message = await bot.send_message(
                    chat_id=user_id,  # 发送到用户私聊
                    text=f"📢 **加入请求验证**\n\n您请求加入群组：**{chat_title}**\n\n{challenge.question}",
                    reply_markup=challenge.keyboard,
                    parse_mode="Markdown",
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
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)

    except Exception as e:
        logger.error(f"处理加入请求失败: {e}")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_user_join(event: ChatMemberUpdated, bot: Bot) -> None:
    """处理用户加入群组事件 - 私聊验证模式

    如果用户已通过加入请求验证，直接欢迎加入，跳过重复验证
    """
    chat_id = event.chat.id
    user = event.new_chat_member.user
    user_id = user.id
    username = user.username or user.full_name

    logger.info(f"用户 {username} ({user_id}) 加入群组 {chat_id}")

    try:
        # 检查用户是否已通过加入请求验证
        redis = get_redis()
        approved_key = RedisKeys.verification_approved(chat_id, user_id)
        is_approved = await redis.get(approved_key)

        if is_approved:
            # 用户已通过验证，直接欢迎（权限已在批准时设置）
            logger.info(f"用户 {user_id} 已通过加入请求验证，跳过重复验证")

            # 清除验证标记
            await redis.delete(approved_key)

            # 获取群组信息
            chat = await bot.get_chat(chat_id)
            chat_title = chat.title or "群组"

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ **验证成功！**\n\n您已成功加入群组：**{chat_title}**\n\n现在可以在群内自由发言了！",
                    parse_mode="Markdown",
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

        # 限制新用户权限
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        logger.info(f"已限制用户 {user_id} 的发言权限")

        # 根据验证类型生成挑战
        verification_service = VerificationService()

        if group.verification_type == "math":
            challenge = await verification_service.generate_math_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "slider":
            challenge = await verification_service.generate_slider_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "qa":
            challenge = await verification_service.generate_qa_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "emoji":
            challenge = await verification_service.generate_emoji_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "captcha":
            challenge = await verification_service.generate_captcha_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "honeypot":
            challenge = await verification_service.generate_honeypot_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "random":
            challenge = await verification_service.generate_random_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        else:  # 默认数学验证
            challenge = await verification_service.generate_math_challenge(
                chat_id, user_id, username, group.verification_timeout
            )

        # ✅ 尝试私聊发送验证消息
        try:
            # 获取群组信息
            chat = await bot.get_chat(chat_id)
            chat_title = chat.title or "群组"

            # 根据是否有图片选择发送方式
            if challenge.photo:
                # captcha 验证：发送图片
                sent_message = await bot.send_photo(
                    chat_id=user_id,
                    photo=challenge.photo,
                    caption=f"📢 **群组验证通知**\n\n您加入了群组：**{chat_title}**\n\n{challenge.question}",
                    reply_markup=challenge.keyboard,
                    parse_mode="Markdown",
                )
            else:
                # 其他验证：发送文本消息
                sent_message = await bot.send_message(
                    chat_id=user_id,  # ✅ 发送到用户私聊
                    text=f"📢 **群组验证通知**\n\n您加入了群组：**{chat_title}**\n\n{challenge.question}",
                    reply_markup=challenge.keyboard,
                    parse_mode="Markdown",
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
            await handle_user_not_started_bot(bot, chat_id, user_id)

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


@router.callback_query(F.data.startswith("verify_math:"))
async def on_math_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理数学验证 - 私聊模式"""
    try:
        _, chat_id_str, user_id_str, answer = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证答案
        verification_service = VerificationService()
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
                await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
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
        _, chat_id_str, user_id_str, position = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证答案
        verification_service = VerificationService()
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
                await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
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
        _, chat_id_str, user_id_str, answer = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证答案
        verification_service = VerificationService()
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
                await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
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
        _, chat_id_str, user_id_str, answer = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证答案
        verification_service = VerificationService()
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
                await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
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
        _, chat_id_str, user_id_str, answer = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证答案
        verification_service = VerificationService()
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
                await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
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

            # 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            logger.info(f"用户 {user_id} 蜜罐验证失败")

    except Exception as e:
        logger.error(f"处理蜜罐验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_captcha_input:"))
async def on_captcha_input_request(callback: CallbackQuery, bot: Bot) -> None:
    """处理验证码输入请求 - 私聊模式"""
    try:
        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 获取群组配置的超时时间
        group_repo = GroupRepository()
        async with get_db_session() as session:
            group_config = await group_repo.get_by_chat_id(session, chat_id)
            timeout = group_config.verification_timeout if group_config else 120

        # 设置等待输入状态（TTL 稍长一点留缓冲）
        redis = get_redis()
        waiting_key = RedisKeys.captcha_waiting(chat_id, user_id)
        await redis.setex(waiting_key, timeout + 10, str(callback.message.message_id))

        await callback.answer("✏️ 请直接发送验证码文本", show_alert=False)

    except Exception as e:
        logger.error(f"处理验证码输入请求失败: {e}")
        await callback.answer("❌ 操作失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_captcha_refresh:"))
async def on_captcha_refresh(callback: CallbackQuery, bot: Bot) -> None:
    """处理验证码刷新 - 私聊模式"""
    try:
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
        async with get_db_session() as session:
            group_config = await group_repo.get_by_chat_id(session, chat_id)
            timeout = group_config.verification_timeout if group_config else 120

        challenge = await verification_service.generate_captcha_challenge(
            chat_id, user_id, username, timeout
        )

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
            reply_markup=challenge.keyboard,
        )
        await callback.answer("🔄 已刷新验证码", show_alert=False)

    except Exception as e:
        logger.error(f"处理验证码刷新失败: {e}")
        await callback.answer("❌ 刷新失败，请联系管理员", show_alert=True)


@router.message(F.chat.type == "private", F.text)
async def on_captcha_text_input(message: Message, bot: Bot) -> None:
    """处理验证码文本输入 - 私聊消息"""
    try:
        user_id = message.from_user.id
        text_input = message.text.strip()

        # 检查是否在等待验证码输入
        redis = get_redis()

        # 查找所有等待该用户输入的验证
        # 遍历可能的群组（通过 RedisKeys 模式匹配）
        # 由于 Redis keys 模式匹配可能影响性能，这里采用更简单的方案：
        # 用户输入后，检查所有验证状态
        pattern = f"verification:*:{user_id}"
        verification_keys = []
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=pattern, count=100)
            verification_keys.extend(keys)
            if cursor == 0:
                break

        if not verification_keys:
            # 没有待验证状态，忽略消息
            return

        # 检查是否有 captcha 类型的验证
        for key in verification_keys:
            stored_value = await redis.get(key)
            if stored_value and stored_value.startswith("captcha:"):
                # 提取 chat_id
                parts = key.split(":")
                if len(parts) >= 3:
                    chat_id = int(parts[1])

                    # 检查是否在等待输入状态
                    waiting_key = RedisKeys.captcha_waiting(chat_id, user_id)
                    message_id_str = await redis.get(waiting_key)

                    if message_id_str:
                        # 验证答案
                        verification_service = VerificationService()
                        if await verification_service.verify_answer(chat_id, user_id, text_input):
                            # 验证成功
                            await redis.delete(waiting_key)

                            # 检查验证类型
                            type_key = RedisKeys.verification_type(chat_id, user_id)
                            verification_type = await redis.get(type_key)
                            is_join_request = verification_type == "join_request"

                            # 清除类型标记
                            await redis.delete(type_key)

                            # 删除验证消息
                            with contextlib.suppress(Exception):
                                await bot.delete_message(
                                    chat_id=user_id, message_id=int(message_id_str)
                                )

                            await message.answer("✅ 验证成功！")

                            # 调用成功处理（需要创建一个 fake callback）
                            # 由于这里是消息处理器，没有 callback，需要直接处理
                            redis = get_redis()
                            type_key = RedisKeys.verification_type(chat_id, user_id)
                            verification_type = await redis.get(type_key)
                            is_join_request = verification_type == "join_request"

                            if is_join_request:
                                # 批准加入请求
                                await bot.approve_chat_join_request(
                                    chat_id=chat_id, user_id=user_id
                                )
                                logger.info(f"用户 {user_id} 验证成功，已批准加入请求")
                            else:
                                # 解除限制
                                await bot.restrict_chat_member(
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    permissions=ChatPermissions(
                                        can_send_messages=True,
                                        can_send_media_messages=True,
                                        can_send_polls=True,
                                        can_send_other_messages=True,
                                        can_add_web_page_previews=True,
                                        can_change_info=False,
                                        can_invite_users=True,
                                        can_pin_messages=False,
                                    ),
                                )

                                # 发送欢迎消息到群组
                                welcome_msg = await bot.send_message(
                                    chat_id=chat_id,
                                    text=f"✅ 欢迎 {message.from_user.mention_html()} 加入群组！",
                                    parse_mode="HTML",
                                )

                                # 30 秒后删除欢迎消息
                                asyncio.create_task(
                                    asyncio.sleep(30)
                                    and bot.delete_message(
                                        chat_id=chat_id, message_id=welcome_msg.message_id
                                    )
                                )

                                logger.info(f"用户 {user_id} 验证成功")

                            # 清除验证状态
                            await verification_service.clear_verification(chat_id, user_id)

                        else:
                            # 验证失败
                            await message.answer("❌ 验证码错误，请重试")
                            await redis.delete(waiting_key)

                            # 根据验证类型决定踢出或拒绝
                            type_key = RedisKeys.verification_type(chat_id, user_id)
                            verification_type = await redis.get(type_key)

                            if verification_type == "join_request":
                                # 拒绝加入请求
                                await bot.decline_chat_join_request(
                                    chat_id=chat_id, user_id=user_id
                                )
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
                                await bot.delete_message(
                                    chat_id=user_id, message_id=int(message_id_str)
                                )

                            logger.info(f"用户 {user_id} 验证码验证失败")

                        # 找到并处理了，退出循环
                        return

    except Exception as e:
        logger.error(f"处理验证码文本输入失败: {e}")


@router.callback_query(F.data.startswith("verify_cancel:"))
async def on_verify_cancel(callback: CallbackQuery, bot: Bot) -> None:
    """处理取消验证 - 私聊模式"""
    try:
        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
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
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id)

        await callback.answer("已取消验证")
        logger.info(f"用户 {user_id} 取消验证，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理取消验证失败: {e}")


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
        chat_title = chat.title or "群组"

        # 清除验证状态
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id)

        # 删除私聊中的验证消息
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)

        if is_join_request:
            # 加入请求模式：批准加入请求
            redis = get_redis()
            approved_key = RedisKeys.verification_approved(chat_id, user_id)

            # 标记用户已验证（60秒有效期，足够用户加入）
            await redis.setex(approved_key, 60, "1")

            # 批准加入请求
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ **验证成功！**\n\n您的加入请求已批准，正在加入群组：**{chat_title}**\n\n稍后您将能在群内自由发言！",
                    parse_mode="Markdown",
                )

            await callback.answer("✅ 验证成功！")
            logger.info(f"用户 {user_id} 加入请求验证成功，已批准加入群组 {chat_id}")

        else:
            # 正常入群模式：恢复群组权限
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )

            # 在私聊中通知用户
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ **验证成功！**\n\n您已成功加入群组：**{chat_title}**\n\n现在可以在群内自由发言了！",
                    parse_mode="Markdown",
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

            # 1. 踢出并封禁 1 小时
            await bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                until_date=datetime.now() + timedelta(hours=1),
            )

            # 2. ✅ 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=message_id)

            # 3. ✅ 在私聊中通知用户（可选）
            try:
                chat = await bot.get_chat(chat_id)
                chat_title = chat.title or "群组"
                await bot.send_message(
                    chat_id=user_id,
                    text=f"❌ **验证超时**\n\n您在群组 **{chat_title}** 的验证已超时，请重新加入并在规定时间内完成验证。",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

            # 4. ✅ 群内不发送任何消息

            # 5. 清除验证状态
            await verification_service.clear_verification(chat_id, user_id)

            logger.info(f"用户 {user_id} 私聊验证超时，已从群组 {chat_id} 踢出（群内无消息）")

    except Exception as e:
        logger.error(f"处理验证超时失败: {e}")


async def handle_user_not_started_bot(bot: Bot, chat_id: int, user_id: int) -> None:
    """
    处理用户未启动 Bot 的情况 - 共享引导消息机制

    优化: 30秒内只发送一条群内引导消息，多用户共享
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
                chat_title = chat.title or "本群组"
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
                    "⚠️ **入群验证提示**\n\n"
                    f"欢迎加入 **{chat_title}**！\n\n"
                    "📱 本群使用 Bot 私聊验证，如果您刚加入但未收到验证消息，"
                    "说明您尚未启动 Bot。\n\n"
                    "👉 请点击下方按钮启动 Bot 进行验证：\n\n"
                    "✅ 启动后会立即收到验证消息\n"
                    "💡 此提示将在 30 秒后自动删除"
                ),
                reply_markup=keyboard,
                parse_mode="Markdown",
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

    # 5. 踢出并封禁当前用户 1 小时
    try:
        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=datetime.now() + timedelta(hours=1),
        )
        logger.info(f"用户 {user_id} 未启动 Bot，已从群组 {chat_id} 踢出并封禁 1 小时")
    except Exception as e:
        logger.error(f"踢出用户失败: {e}")


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

            # 1. 拒绝加入请求并封禁1小时，防止立即重试
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            await bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                until_date=datetime.now() + timedelta(hours=1),
            )

            # 2. 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=message_id)

            # 3. 在私聊中通知用户（可选）
            try:
                chat = await bot.get_chat(chat_id)
                chat_title = chat.title or "群组"
                await bot.send_message(
                    chat_id=user_id,
                    text=f"❌ **验证超时**\n\n您加入群组 **{chat_title}** 的请求已被拒绝，原因：验证超时。\n\n请重新发送加入请求并在规定时间内完成验证。",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

            # 4. 清除验证状态
            await verification_service.clear_verification(chat_id, user_id)

            # 5. 清除验证类型标记
            redis = get_redis()
            type_key = RedisKeys.verification_type(chat_id, user_id)
            await redis.delete(type_key)

            logger.info(f"用户 {user_id} 加入请求验证超时，已拒绝加入群组 {chat_id}")

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
                chat_title = chat.title or "本群组"
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
                    "⚠️ **入群验证提示**\n\n"
                    f"欢迎请求加入 **{chat_title}**！\n\n"
                    "📱 本群使用 Bot 私聊验证，如果您发送了加入请求但未收到验证消息，"
                    "说明您尚未启动 Bot。\n\n"
                    "👉 请点击下方按钮启动 Bot 后重新发送加入请求：\n\n"
                    "✅ 启动后重新请求加入即可收到验证消息\n"
                    "💡 此提示将在 30 秒后自动删除"
                ),
                reply_markup=keyboard,
                parse_mode="Markdown",
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
        await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        logger.info(f"用户 {user_id} 未启动 Bot，已拒绝加入请求（群组 {chat_id}）")
    except Exception as e:
        logger.error(f"拒绝加入请求失败: {e}")

    # 6. 清除验证类型标记
    type_key = RedisKeys.verification_type(chat_id, user_id)
    await redis.delete(type_key)
