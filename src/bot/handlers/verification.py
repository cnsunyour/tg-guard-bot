"""入群验证处理器"""

import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, ChatPermissions
from loguru import logger

from src.core.utils import format_user_mention
from src.repositories.group_repo import GroupRepository
from src.services.verification import VerificationService

router = Router(name="verification")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_user_join(event: ChatMemberUpdated, bot: Bot) -> None:
    """处理用户加入群组事件"""
    chat_id = event.chat.id
    user = event.new_chat_member.user
    user_id = user.id
    username = user.username or user.full_name

    logger.info(f"用户 {username} ({user_id}) 加入群组 {chat_id}")

    try:
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

        # ✅ P1-7: 传入群组配置的验证超时时间
        if group.verification_type == "math":
            challenge = await verification_service.generate_math_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        elif group.verification_type == "slider":
            challenge = await verification_service.generate_slider_challenge(
                chat_id, user_id, username, group.verification_timeout
            )
        else:  # 默认按钮验证
            challenge = await verification_service.generate_button_challenge(
                chat_id, user_id, username, group.verification_timeout
            )

        # 发送验证消息
        sent_message = await bot.send_message(
            chat_id=chat_id, text=challenge.question, reply_markup=challenge.keyboard
        )

        # 启动超时处理任务
        asyncio.create_task(
            handle_verification_timeout(
                bot, chat_id, user_id, sent_message.message_id, group.verification_timeout
            )
        )

    except Exception as e:
        logger.error(f"处理用户加入事件失败: {e}")


@router.callback_query(F.data.startswith("verify_btn:"))
async def on_button_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理按钮验证"""
    try:
        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # ✅ M4: 验证 callback 上下文与 callback_data 一致
        if callback.message and callback.message.chat.id != chat_id:
            await callback.answer("❌ 验证上下文不匹配", show_alert=True)
            logger.warning(
                f"回调上下文不匹配: callback_data chat_id={chat_id}, "
                f"实际 chat_id={callback.message.chat.id}"
            )
            return

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证通过
        verification_service = VerificationService()
        if await verification_service.verify_answer(chat_id, user_id, "button"):
            await handle_verification_success(bot, callback, chat_id, user_id)
        else:
            await callback.answer("❌ 验证已过期", show_alert=True)

    except Exception as e:
        logger.error(f"处理按钮验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_math:"))
async def on_math_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理数学验证"""
    try:
        _, chat_id_str, user_id_str, answer = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # ✅ M4: 验证 callback 上下文与 callback_data 一致
        if callback.message and callback.message.chat.id != chat_id:
            await callback.answer("❌ 验证上下文不匹配", show_alert=True)
            logger.warning(
                f"回调上下文不匹配: callback_data chat_id={chat_id}, "
                f"实际 chat_id={callback.message.chat.id}"
            )
            return

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证答案
        verification_service = VerificationService()
        if await verification_service.verify_answer(chat_id, user_id, answer):
            await handle_verification_success(bot, callback, chat_id, user_id)
        else:
            await callback.answer("❌ 答案错误，请重试", show_alert=True)
            # 踢出用户
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
            logger.info(f"用户 {user_id} 验证失败，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理数学验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_slider:"))
async def on_slider_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理滑块验证"""
    try:
        _, chat_id_str, user_id_str, position = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # ✅ M4: 验证 callback 上下文与 callback_data 一致
        if callback.message and callback.message.chat.id != chat_id:
            await callback.answer("❌ 验证上下文不匹配", show_alert=True)
            logger.warning(
                f"回调上下文不匹配: callback_data chat_id={chat_id}, "
                f"实际 chat_id={callback.message.chat.id}"
            )
            return

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 验证答案
        verification_service = VerificationService()
        if await verification_service.verify_answer(chat_id, user_id, position):
            await handle_verification_success(bot, callback, chat_id, user_id)
        else:
            await callback.answer("❌ 选择错误，请重试", show_alert=True)
            # 踢出用户
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
            logger.info(f"用户 {user_id} 验证失败，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理滑块验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


@router.callback_query(F.data.startswith("verify_cancel:"))
async def on_verify_cancel(callback: CallbackQuery, bot: Bot) -> None:
    """处理取消验证"""
    try:
        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # ✅ M4: 验证 callback 上下文与 callback_data 一致
        if callback.message and callback.message.chat.id != chat_id:
            await callback.answer("❌ 验证上下文不匹配", show_alert=True)
            logger.warning(
                f"回调上下文不匹配: callback_data chat_id={chat_id}, "
                f"实际 chat_id={callback.message.chat.id}"
            )
            return

        # 检查是否是本人点击
        if callback.from_user.id != user_id:
            await callback.answer("❌ 这不是你的验证消息", show_alert=True)
            return

        # 踢出用户
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)

        # 清除验证状态
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id)

        await callback.answer("已取消验证")
        logger.info(f"用户 {user_id} 取消验证，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理取消验证失败: {e}")


async def handle_verification_success(
    bot: Bot, callback: CallbackQuery, chat_id: int, user_id: int
) -> None:
    """处理验证成功"""
    try:
        # 恢复用户权限
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

        # 清除验证状态
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id)

        # 删除验证消息
        await bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)

        # 发送欢迎消息
        welcome_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"✅ 验证成功！欢迎 {format_user_mention(callback.from_user)} 加入群组！",
        )

        # 5秒后删除欢迎消息
        await asyncio.sleep(5)
        await bot.delete_message(chat_id=chat_id, message_id=welcome_msg.message_id)

        await callback.answer("✅ 验证成功！")
        logger.info(f"用户 {user_id} 验证成功，已恢复权限")

    except Exception as e:
        logger.error(f"处理验证成功失败: {e}")


async def handle_verification_timeout(
    bot: Bot, chat_id: int, user_id: int, message_id: int, timeout: int
) -> None:
    """处理验证超时"""
    try:
        # 等待超时时间
        await asyncio.sleep(timeout)

        # 检查验证状态
        verification_service = VerificationService()
        if await verification_service.is_verification_pending(chat_id, user_id):
            # 验证超时，踢出用户
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)

            # 清除验证状态
            await verification_service.clear_verification(chat_id, user_id)

            logger.info(f"用户 {user_id} 验证超时，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理验证超时失败: {e}")
