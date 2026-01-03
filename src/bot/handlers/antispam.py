"""反垃圾消息处理器"""

import tempfile
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from loguru import logger

from src.services.spam_detector import get_detector
from src.services.moderation import ModerationService
from src.repositories.group_repo import GroupRepository
from src.core.config import settings
from src.core.utils import format_user_mention
from src.core.cache import PermissionCache  # ✅ P1-10: 导入权限缓存
from src.core.redis import get_redis, RedisKeys  # ✅ P1-12: 导入 Redis 和键管理

router = Router(name="antispam")


@contextmanager
def managed_temp_file(suffix: str = ".jpg") -> Iterator[str]:
    """✅ M8: 上下文管理器确保临时文件一定会被清理

    Args:
        suffix: 文件后缀

    Yields:
        临时文件路径

    注意：无论是否发生异常，临时文件都会在退出时被删除
    """
    temp_file_path = None
    try:
        # 创建临时文件
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            temp_file_path = tf.name
        logger.debug(f"创建临时文件: {temp_file_path}")
        yield temp_file_path
    finally:
        # 确保清理临时文件
        if temp_file_path:
            try:
                temp_path = Path(temp_file_path)
                if temp_path.exists():
                    temp_path.unlink()
                    logger.debug(f"临时文件已删除: {temp_file_path}")
            except Exception as e:
                logger.error(f"删除临时文件失败 {temp_file_path}: {e}")


@router.message(F.text)
async def on_message(message: Message, bot: Bot) -> None:
    """处理所有文本消息，检测垃圾"""
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过管理员消息
    if message.from_user.id in settings.admin_ids:
        return

    # ✅ P1-10: 使用 Redis 缓存减少 API 调用
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组是否启用反垃圾
    try:
        group = await GroupRepository.get(message.chat.id)
        if group and not group.antispam_enabled:
            return
    except Exception as e:
        # ✅ L6: 添加日志，不静默吞掉异常
        logger.debug(f"检查管理员权限失败（非关键）: {e}")

    # 获取检测器
    detector = get_detector()

    # 检测垃圾
    result = await detector.detect(
        text=message.text,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
    )

    # 如果检测到垃圾
    if result["is_spam"]:
        logger.warning(
            f"检测到垃圾信息 [群组:{message.chat.id}] "
            f"[用户:{message.from_user.id}] "
            f"阶段: {result['stage']}, "
            f"置信度: {result['confidence']:.2f}, "
            f"原因: {', '.join(result['reasons'])}"
        )

        try:
            # 删除垃圾消息
            await message.delete()

            # 禁言用户 (10分钟)
            # ✅ P1-6: 正确处理 mute_user 的返回值 (bool, str)
            success, error_msg = await ModerationService.mute_user(
                bot=bot,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                operator_id=bot.id,  # Bot 作为操作者
                duration=10,  # 10分钟
                reason=f"垃圾信息: {', '.join(result['reasons'])}",
            )

            if success:
                # ✅ P1-12: 缓存原始消息文本，用于管理员反馈
                # TTL 1小时，因为管理员通常会很快反馈
                redis = get_redis()
                text_cache_key = RedisKeys.spam_message_text(message.chat.id, message.message_id)
                await redis.setex(text_cache_key, 3600, message.text)

                # 发送提示消息（包含管理员反馈按钮）
                # ✅ 使用 message_id 代替文本内容，避免注入风险
                message_id_str = str(message.message_id)

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ 误判",
                                callback_data=f"spam_feedback:normal:{message.from_user.id}:{message_id_str}",
                            ),
                            InlineKeyboardButton(
                                text="❌ 确认垃圾",
                                callback_data=f"spam_feedback:spam:{message.from_user.id}:{message_id_str}",
                            ),
                        ]
                    ]
                )

                alert_msg = await message.answer(
                    f"🚫 检测到垃圾信息并已处理\n\n"
                    f"用户: {format_user_mention(message.from_user)}\n"
                    f"原因: {', '.join(result['reasons'])}\n"
                    f"置信度: {result['confidence']:.2%}\n"
                    f"处罚: 禁言 10 分钟",
                    reply_markup=keyboard,
                )

                # 记录原始消息文本用于反馈
                await detector.add_feedback(
                    text=message.text,
                    is_spam=True,
                    labeled_by=bot.id,
                    confidence=result["confidence"],
                )

            else:
                # ✅ P1-6: 处理禁言失败情况
                logger.error(f"禁言垃圾用户失败: {error_msg}")

        except Exception as e:
            logger.error(f"处理垃圾消息失败: {e}")


@router.message(F.photo)
async def on_photo_message(message: Message, bot: Bot) -> None:
    """处理图片消息，检测垃圾"""
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过管理员消息
    if message.from_user.id in settings.admin_ids:
        return

    # ✅ P1-10: 使用 Redis 缓存减少 API 调用
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组是否启用反垃圾
    try:
        group = await GroupRepository.get(message.chat.id)
        if group and not group.antispam_enabled:
            return
    except Exception as e:
        # ✅ L6: 添加日志，不静默吞掉异常
        logger.debug(f"检查管理员权限失败（非关键）: {e}")

    # 获取检测器
    detector = get_detector()

    # 检查 OCR 是否可用
    if not detector.ocr_extractor.is_available:
        logger.debug("OCR 不可用，跳过图片检测")
        return

    # ✅ M8: 使用 context manager 确保临时文件一定会被清理
    try:
        # 获取最大的图片（最后一个）
        photo = message.photo[-1]

        # 使用 managed_temp_file context manager 确保清理
        with managed_temp_file(suffix=".jpg") as temp_file_path:
            # 下载图片到临时文件
            await bot.download(photo, destination=temp_file_path)
            logger.debug(f"图片已下载到临时文件: {temp_file_path}")

            # 检测图片
            result = await detector.detect_image(
                image_path=temp_file_path,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
            )
        # 注意：临时文件在退出 with 块时自动删除

        # 如果检测到垃圾
        if result["is_spam"]:
            logger.warning(
                f"检测到图片垃圾信息 [群组:{message.chat.id}] "
                f"[用户:{message.from_user.id}] "
                f"阶段: {result['stage']}, "
                f"置信度: {result['confidence']:.2f}, "
                f"原因: {', '.join(result['reasons'])}"
            )

            try:
                # 删除垃圾消息
                await message.delete()

                # 禁言用户 (10分钟)
                success, error_msg = await ModerationService.mute_user(
                    bot=bot,
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    operator_id=bot.id,
                    duration=10,
                    reason=f"图片垃圾信息: {', '.join(result['reasons'])}",
                )

                if success:
                    # ✅ P1-12: 缓存原始消息的 OCR 文本，用于管理员反馈
                    # TTL 1小时
                    if "ocr_text" in result["details"]:
                        redis = get_redis()
                        text_cache_key = RedisKeys.spam_message_text(message.chat.id, message.message_id)
                        await redis.setex(text_cache_key, 3600, result["details"]["ocr_text"])

                    # 发送提示消息（不包含 OCR 提取的敏感内容）
                    # ⚠️ 注意：callback_data 中也不应包含敏感信息，应使用 message_id 作为标识
                    message_id_str = str(message.message_id)

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ 误判",
                                    callback_data=f"spam_feedback:normal:{message.from_user.id}:{message_id_str}",
                                ),
                                InlineKeyboardButton(
                                    text="❌ 确认垃圾",
                                    callback_data=f"spam_feedback:spam:{message.from_user.id}:{message_id_str}",
                                ),
                            ]
                        ]
                    )

                    await message.answer(
                        f"🚫 检测到图片垃圾信息并已处理\n\n"
                        f"用户: {format_user_mention(message.from_user)}\n"
                        f"原因: {', '.join(result['reasons'])}\n"
                        f"置信度: {result['confidence']:.2%}\n"
                        f"处罚: 禁言 10 分钟",
                        reply_markup=keyboard,
                    )

                    # 记录反馈（可选）
                    if "ocr_text" in result["details"]:
                        await detector.add_feedback(
                            text=result["details"]["ocr_text"],
                            is_spam=True,
                            labeled_by=bot.id,
                            confidence=result["confidence"],
                        )
                else:
                    logger.error(f"禁言用户失败: {error_msg}")

            except Exception as e:
                logger.error(f"处理图片垃圾消息失败: {e}")

    except Exception as e:
        logger.error(f"图片检测失败: {e}")


@router.callback_query(F.data.startswith("spam_feedback:"))
async def on_spam_feedback(callback: CallbackQuery) -> None:
    """处理管理员反馈

    ✅ P1-12: 从 Redis 缓存获取真实文本，而非使用 message_id
    """
    try:
        _, feedback_type, user_id_str, message_id_str = callback.data.split(":", 3)

        # 检查是否是管理员
        if callback.from_user.id not in settings.admin_ids:
            # ✅ P1-10: 使用 Redis 缓存减少 API 调用
            if not await PermissionCache.is_admin(
                callback.bot, callback.message.chat.id, callback.from_user.id
            ):
                await callback.answer("❌ 只有管理员可以提供反馈", show_alert=True)
                return

        detector = get_detector()

        # 根据反馈类型更新样本
        is_spam = feedback_type == "spam"

        # ✅ P1-12: 从 Redis 获取缓存的原始消息文本
        redis = get_redis()
        text_cache_key = RedisKeys.spam_message_text(
            callback.message.chat.id, int(message_id_str)
        )
        cached_text = await redis.get(text_cache_key)

        if cached_text:
            # 使用缓存的真实文本
            await detector.add_feedback(
                text=cached_text,
                is_spam=is_spam,
                labeled_by=callback.from_user.id,
            )
            logger.debug(f"使用缓存文本添加反馈 [消息ID:{message_id_str}] [长度:{len(cached_text)}]")
        else:
            # 缓存已过期或不存在，记录警告但仍然接受反馈
            logger.warning(
                f"反馈文本缓存未命中 [消息ID:{message_id_str}]，"
                "可能是缓存过期或系统重启导致"
            )
            await callback.answer("⚠️ 原始文本已过期，反馈可能不完整", show_alert=True)
            return

        # 更新消息
        feedback_text = "✅ 确认为正常消息" if not is_spam else "❌ 确认为垃圾信息"
        await callback.message.edit_text(
            callback.message.text + f"\n\n{feedback_text} (by {format_user_mention(callback.from_user)})"
        )

        await callback.answer(f"反馈已记录: {feedback_text}")

        logger.info(
            f"管理员反馈 [管理员:{callback.from_user.id}] "
            f"类型: {'垃圾' if is_spam else '正常'}"
        )

    except Exception as e:
        logger.error(f"处理管理员反馈失败: {e}")
        await callback.answer("❌ 处理失败", show_alert=True)


@router.message(Command("antispam"))
async def cmd_antispam(message: Message, bot: Bot) -> None:
    """反垃圾配置命令"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if message.from_user.id not in settings.admin_ids:
        # ✅ P1-10: 使用 Redis 缓存减少 API 调用
        if not await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
            await message.answer("❌ 只有管理员可以使用此命令")
            return

    # 显示配置菜单
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 启用反垃圾",
                    callback_data=f"antispam_toggle:{message.chat.id}:on",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ 禁用反垃圾",
                    callback_data=f"antispam_toggle:{message.chat.id}:off",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 查看统计",
                    callback_data=f"antispam_stats:{message.chat.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 重新训练模型",
                    callback_data=f"antispam_retrain:{message.chat.id}",
                )
            ],
        ]
    )

    await message.answer("🛡️ 反垃圾配置", reply_markup=keyboard)


@router.callback_query(F.data.startswith("antispam_toggle:"))
async def on_antispam_toggle(callback: CallbackQuery) -> None:
    """处理反垃圾开关"""
    try:
        _, chat_id_str, action = callback.data.split(":")
        chat_id = int(chat_id_str)

        # ✅ 权限验证
        if callback.from_user.id not in settings.admin_ids:
            # ✅ P1-10: 使用 Redis 缓存减少 API 调用
            if not await PermissionCache.is_admin(callback.bot, chat_id, callback.from_user.id):
                await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
                logger.warning(
                    f"用户 {callback.from_user.id} 尝试修改群组 {chat_id} 反垃圾设置但无权限"
                )
                return

        # ✅ 参数白名单验证
        if action not in ["on", "off"]:
            await callback.answer("❌ 无效的操作", show_alert=True)
            logger.warning(f"无效的反垃圾操作: {action}")
            return

        enabled = action == "on"

        await GroupRepository.update_antispam_settings(chat_id, enabled)

        status = "已启用" if enabled else "已禁用"
        await callback.message.edit_text(f"✅ 反垃圾功能{status}")
        await callback.answer(f"反垃圾{status}")

        logger.info(f"群组 {chat_id} 反垃圾功能{status}")

    except Exception as e:
        logger.error(f"切换反垃圾失败: {e}")
        await callback.answer("❌ 操作失败", show_alert=True)


@router.callback_query(F.data.startswith("antispam_stats:"))
async def on_antispam_stats(callback: CallbackQuery) -> None:
    """查看反垃圾统计"""
    try:
        detector = get_detector()
        stats = await detector.get_statistics()

        text = (
            f"📊 <b>反垃圾统计</b>\n\n"
            f"总样本数: {stats.get('total_samples', 0)}\n"
            f"垃圾样本: {stats.get('spam_samples', 0)}\n"
            f"正常样本: {stats.get('normal_samples', 0)}\n\n"
            f"ML 分类器: {'✅ 已训练' if stats.get('classifier_trained') else '❌ 未训练'}\n"
            f"Embedding: {'✅ 已初始化' if stats.get('embedder_initialized') else '❌ 未初始化'}"
        )

        await callback.message.edit_text(text)
        await callback.answer("统计信息已更新")

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        await callback.answer("❌ 获取失败", show_alert=True)


@router.callback_query(F.data.startswith("antispam_retrain:"))
async def on_antispam_retrain(callback: CallbackQuery) -> None:
    """重新训练模型"""
    try:
        _, chat_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)

        # ✅ 权限验证 - 重训练是敏感操作，仅超级管理员可执行
        if callback.from_user.id not in settings.admin_ids:
            await callback.answer("❌ 只有超级管理员可以重新训练模型", show_alert=True)
            logger.warning(
                f"用户 {callback.from_user.id} 尝试触发模型重训练但无权限"
            )
            return

        await callback.answer("正在训练模型，请稍候...")

        detector = get_detector()
        success, message = await detector.retrain_model()

        if success:
            await callback.message.edit_text(f"✅ {message}")
        else:
            await callback.message.edit_text(f"❌ {message}")

    except Exception as e:
        logger.error(f"重新训练模型失败: {e}")
        await callback.answer("❌ 训练失败", show_alert=True)
