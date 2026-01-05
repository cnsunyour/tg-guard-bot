"""入群验证处理器"""

import asyncio
import contextlib

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from loguru import logger

from src.core.redis import RedisKeys, get_redis
from src.core.utils import format_user_mention
from src.repositories.group_repo import GroupRepository
from src.services.verification import VerificationService

router = Router(name="verification")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_user_join(event: ChatMemberUpdated, bot: Bot) -> None:
    """处理用户加入群组事件 - 私聊验证模式"""
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

        # ✅ 尝试私聊发送验证消息
        try:
            # 获取群组信息
            chat = await bot.get_chat(chat_id)
            chat_title = chat.title or "群组"

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
            # 踢出用户
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)

    except Exception as e:
        logger.error(f"处理用户加入事件失败: {e}")


@router.callback_query(F.data.startswith("verify_btn:"))
async def on_button_verify(callback: CallbackQuery, bot: Bot) -> None:
    """处理按钮验证 - 私聊模式"""
    try:
        _, chat_id_str, user_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

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
            await handle_verification_success(bot, callback, chat_id, user_id)
        else:
            await callback.answer("❌ 答案错误", show_alert=True)
            # 踢出用户
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            # 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            logger.info(f"用户 {user_id} 验证失败，已被踢出群组 {chat_id}")

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
            await handle_verification_success(bot, callback, chat_id, user_id)
        else:
            await callback.answer("❌ 选择错误", show_alert=True)
            # 踢出用户
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            # 删除私聊中的验证消息
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)
            logger.info(f"用户 {user_id} 验证失败，已被踢出群组 {chat_id}")

    except Exception as e:
        logger.error(f"处理滑块验证失败: {e}")
        await callback.answer("❌ 验证失败，请联系管理员", show_alert=True)


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

        # 踢出用户
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)

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
    bot: Bot, callback: CallbackQuery, chat_id: int, user_id: int
) -> None:
    """处理验证成功 - 私聊验证模式"""
    try:
        # 1. 恢复群组权限
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

        # 2. 获取群组信息
        chat = await bot.get_chat(chat_id)
        chat_title = chat.title or "群组"

        # 3. 清除验证状态
        verification_service = VerificationService()
        await verification_service.clear_verification(chat_id, user_id)

        # 4. ✅ 删除私聊中的验证消息
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=user_id, message_id=callback.message.message_id)

        # 5. ✅ 在私聊中通知用户
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ **验证成功！**\n\n您已成功加入群组：**{chat_title}**\n\n现在可以在群内自由发言了！",
                parse_mode="Markdown",
            )

        # 6. ✅ 在群内发送欢迎消息（仅此一条群内消息）
        welcome_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"✅ 欢迎 {format_user_mention(callback.from_user)} 加入群组！",
        )

        # 7. 5秒后删除欢迎消息
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

            # 1. 踢出用户
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)

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

    # 5. 踢出当前用户（允许重新加入）
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        logger.info(f"用户 {user_id} 未启动 Bot，已从群组 {chat_id} 踢出")
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
