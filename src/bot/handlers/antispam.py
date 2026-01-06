"""反垃圾消息处理器"""

import gzip
import json
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import imageio.v3 as iio
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger
from PIL import Image

from src.core.cache import PermissionCache  # ✅ P1-10: 导入权限缓存
from src.core.config import settings
from src.core.redis import RedisKeys, get_redis  # ✅ P1-12: 导入 Redis 和键管理
from src.core.utils import auto_delete_message, check_admin_permission, format_user_mention
from src.repositories.group_repo import GroupRepository
from src.services.moderation import ModerationService
from src.services.spam_detector import get_detector

router = Router(name="antispam")

# 已注册的命令集合（将在 bot 启动时自动从 dispatcher 中提取）
_registered_commands: set[str] = set()


def set_registered_commands(commands: set[str]) -> None:
    """设置已注册的命令列表（由 main.py 在启动时调用）

    Args:
        commands: 从 dispatcher 中提取的所有命令名集合
    """
    global _registered_commands
    _registered_commands = commands
    logger.info(f"已注册 {len(commands)} 个命令到反垃圾白名单: {sorted(commands)}")


def get_registered_commands() -> set[str]:
    """获取已注册的命令列表"""
    return _registered_commands


def is_anonymous_admin(message: Message) -> bool:
    """检查消息是否来自匿名管理员

    当管理员以"匿名管理员"身份发言时：
    - message.sender_chat 不为 None
    - message.sender_chat.id == message.chat.id (发送者是群组本身)

    Args:
        message: 消息对象

    Returns:
        是否是匿名管理员消息
    """
    return message.sender_chat is not None and message.sender_chat.id == message.chat.id


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


@router.message(Command("antispam"))
async def cmd_antispam(message: Message, bot: Bot) -> None:
    """反垃圾配置命令"""
    logger.debug(
        f"收到 /antispam 命令 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
        f"[chat_type:{message.chat.type}] [from_user:{message.from_user.username}] "
        f"[sender_chat:{message.sender_chat.id if message.sender_chat else None}]"
    )

    # 检查是否在群组中
    if message.chat.type == "private":
        logger.debug("私聊模式，拒绝执行")
        reply = await message.answer("❌ 此命令只能在群组中使用")
        await auto_delete_message(reply)
        return

    # 检查权限（使用统一的权限检查函数）
    if not await check_admin_permission(message, bot):
        reply = await message.answer("❌ 只有管理员可以使用此命令")
        await auto_delete_message(reply)
        return

    logger.debug("权限检查通过，准备发送配置菜单")

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

    logger.debug("发送配置菜单消息")
    reply = await message.answer("🛡️ 反垃圾配置", reply_markup=keyboard)
    logger.debug(f"配置菜单已发送，消息ID: {reply.message_id}")
    await auto_delete_message(reply)


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
        int(chat_id_str)

        # ✅ 权限验证 - 重训练是敏感操作，仅超级管理员可执行
        if callback.from_user.id not in settings.admin_ids:
            await callback.answer("❌ 只有超级管理员可以重新训练模型", show_alert=True)
            logger.warning(f"用户 {callback.from_user.id} 尝试触发模型重训练但无权限")
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


@router.message(F.text)
async def on_message(message: Message, bot: Bot) -> None:
    """处理所有文本消息，检测垃圾"""
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过已注册的命令消息
    if message.text.startswith("/"):
        # 提取命令名（格式：/command 或 /command@botname 或 /command args）
        command_match = re.match(r"^/([a-zA-Z][a-zA-Z0-9_]*)(@\w+)?(\s|$)", message.text)
        if command_match:
            command_name = command_match.group(1)
            # 只跳过已注册的命令
            if command_name in _registered_commands:
                logger.debug(
                    f"[文本处理器] 跳过已注册命令 [群组:{message.chat.id}] "
                    f"[命令:{command_name}]"
                )
                return
            # 未注册的命令格式文本（如 /abc spam）会继续进行垃圾检测
            logger.debug(
                f"检测到未注册命令格式的消息 [群组:{message.chat.id}] "
                f"[命令:{command_name}]，将进行垃圾检测"
            )

    # 跳过匿名管理员消息
    if is_anonymous_admin(message):
        logger.debug(f"跳过匿名管理员文本消息 [群组:{message.chat.id}]")
        return

    # 跳过超级管理员消息
    if message.from_user.id in settings.admin_ids:
        logger.debug(f"跳过超级管理员文本消息 [用户:{message.from_user.id}]")
        return

    # ✅ P1-10: 使用 Redis 缓存减少 API 调用
    # 跳过群组管理员消息
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        logger.debug(
            f"跳过群组管理员文本消息 [群组:{message.chat.id}] [用户:{message.from_user.id}]"
        )
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
                await auto_delete_message(alert_msg)

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

    # 跳过匿名管理员消息
    if is_anonymous_admin(message):
        logger.debug(f"跳过匿名管理员图片消息 [群组:{message.chat.id}]")
        return

    # 跳过超级管理员消息
    if message.from_user.id in settings.admin_ids:
        logger.debug(f"跳过超级管理员图片消息 [用户:{message.from_user.id}]")
        return

    # ✅ P1-10: 使用 Redis 缓存减少 API 调用
    # 跳过群组管理员消息
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        logger.debug(
            f"跳过群组管理员图片消息 [群组:{message.chat.id}] [用户:{message.from_user.id}]"
        )
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
                        text_cache_key = RedisKeys.spam_message_text(
                            message.chat.id, message.message_id
                        )
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

                    alert_msg = await message.answer(
                        f"🚫 检测到图片垃圾信息并已处理\n\n"
                        f"用户: {format_user_mention(message.from_user)}\n"
                        f"原因: {', '.join(result['reasons'])}\n"
                        f"置信度: {result['confidence']:.2%}\n"
                        f"处罚: 禁言 10 分钟",
                        reply_markup=keyboard,
                    )
                    await auto_delete_message(alert_msg)

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


@router.message(F.sticker)
async def on_sticker_message(message: Message, bot: Bot) -> None:
    """处理贴纸消息，检测垃圾"""
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过匿名管理员消息
    if is_anonymous_admin(message):
        logger.debug(f"跳过匿名管理员贴纸消息 [群组:{message.chat.id}]")
        return

    # 跳过超级管理员消息
    if message.from_user.id in settings.admin_ids:
        logger.debug(f"跳过超级管理员贴纸消息 [用户:{message.from_user.id}]")
        return

    # 跳过群组管理员消息
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        logger.debug(
            f"跳过群组管理员贴纸消息 [群组:{message.chat.id}] [用户:{message.from_user.id}]"
        )
        return

    # 检查群组是否启用反垃圾
    try:
        group = await GroupRepository.get(message.chat.id)
        if group and not group.antispam_enabled:
            return
    except Exception as e:
        logger.debug(f"检查群组配置失败（非关键）: {e}")

    # 获取检测器
    detector = get_detector()

    # 检查 OCR 是否可用
    if not detector.ocr_extractor.is_available:
        logger.debug("OCR 不可用，跳过贴纸检测")
        return

    # 使用 context manager 确保临时文件清理
    try:
        # 下载贴纸文件
        sticker = message.sticker

        # ✅ 检查贴纸类型
        # 处理动画 TGS 贴纸
        if sticker.is_animated:
            # 懒加载 lottie（仅在 OCR 启用时可用）
            try:
                from lottie.exporters.cairo import export_png
                from lottie.importers.core import import_tgs
            except ImportError as e:
                logger.debug(f"TGS 支持不可用（需要安装 OCR 依赖）: {e}")
                return

            with managed_temp_file(suffix=".tgs") as tgs_file_path:
                # 下载贴纸
                await bot.download(sticker, destination=tgs_file_path)
                logger.debug(
                    f"动画贴纸已下载: {tgs_file_path}, "
                    f"大小: {sticker.width}x{sticker.height}, "
                    f"文件大小: {sticker.file_size} bytes"
                )

                # 提取首帧和中间帧进行检测
                try:
                    # TGS = gzip-compressed Lottie JSON
                    tgs_path = Path(tgs_file_path)

                    # ✅ 防止 gzip 炸弹攻击：限制解压后大小为 10MB
                    MAX_DECOMPRESSED_SIZE = 10 * 1024 * 1024
                    compressed_data = tgs_path.read_bytes()
                    decompressed_data = gzip.decompress(compressed_data)

                    if len(decompressed_data) > MAX_DECOMPRESSED_SIZE:
                        logger.warning(
                            f"TGS 文件解压后过大: {len(decompressed_data)} bytes (限制: {MAX_DECOMPRESSED_SIZE})"
                        )
                        return

                    meta = json.loads(decompressed_data)

                    # ✅ 使用 float 解析并处理边界情况
                    ip = float(meta.get("ip", 0))  # in point (起始帧)
                    op = float(meta.get("op", ip + 1))  # out point (结束帧)

                    # ✅ 验证帧范围
                    if op <= ip:
                        logger.warning(f"无效的 TGS 帧范围: ip={ip}, op={op}")
                        return

                    total_frames = int(op - ip)
                    logger.debug(f"TGS 动画总帧数: {total_frames} (ip={ip}, op={op})")

                    if total_frames <= 0:
                        logger.warning("TGS 动画无有效帧")
                        return

                    # 确定检测帧索引：1/3 帧 + 2/3 帧
                    check_indices = [int(ip) + total_frames // 3]  # 1/3 帧
                    if total_frames > 2:
                        check_indices.append(int(ip) + total_frames * 2 // 3)  # 2/3 帧

                    logger.debug(f"将检测第 {check_indices} 帧 (1/3 和 2/3 位置)")

                    # 导入 TGS 动画
                    anim = import_tgs(str(tgs_file_path))

                    # 循环检测每一帧
                    for frame_idx in check_indices:
                        logger.debug(f"渲染第 {frame_idx} 帧 (共{total_frames}帧)")

                        with managed_temp_file(suffix=".png") as png_file_path:
                            # 渲染当前帧为 PNG
                            export_png(anim, png_file_path, frame=frame_idx)
                            logger.debug(f"第 {frame_idx} 帧已渲染为 PNG: {png_file_path}")

                            # ✅ 使用 context manager 打开图片并全面处理颜色模式
                            with Image.open(png_file_path) as img:
                                # 转换为 RGB（OCR 需要）
                                if img.mode in ("RGBA", "LA", "P"):
                                    # 将透明背景转为白色
                                    background = Image.new("RGB", img.size, (255, 255, 255))
                                    if img.mode == "P":
                                        img = img.convert("RGBA")
                                    if img.mode in ("RGBA", "LA"):
                                        background.paste(
                                            img, mask=img.split()[-1]  # alpha channel
                                        )
                                    else:
                                        background.paste(img)
                                    background.save(png_file_path, "PNG")
                                elif img.mode != "RGB":
                                    # 其他模式直接转 RGB
                                    img.convert("RGB").save(png_file_path, "PNG")

                            # 检测当前帧中的文字
                            result = await detector.detect_image(
                                image_path=png_file_path,
                                user_id=message.from_user.id,
                                chat_id=message.chat.id,
                            )

                            # 如果检测到垃圾，立即停止检测
                            if result["is_spam"]:
                                logger.info(f"第 {frame_idx} 帧检测到垃圾，停止后续检测")
                                break

                except Exception as e:
                    # ✅ 使用 logger.exception 保留堆栈跟踪
                    logger.exception(f"TGS 动画帧渲染失败: {e}")
                    return

        # 处理静态 WebP 贴纸
        elif not sticker.is_video:
            with managed_temp_file(suffix=".webp") as webp_file_path:
                # 下载贴纸到临时文件
                await bot.download(sticker, destination=webp_file_path)
                logger.debug(
                    f"静态贴纸已下载: {webp_file_path}, "
                    f"大小: {sticker.width}x{sticker.height}, "
                    f"文件大小: {sticker.file_size} bytes"
                )

                # 将 WebP 转换为 PNG（PaddleOCR 不支持 WebP）
                with managed_temp_file(suffix=".png") as png_file_path:
                    try:
                        # ✅ 检查文件内容
                        with open(webp_file_path, "rb") as f:
                            header = f.read(16)
                            logger.debug(f"文件头部: {header[:12].hex()}")
                            # WebP 文件应该以 "RIFF" 开头，并包含 "WEBP"
                            if not (header[:4] == b"RIFF" and header[8:12] == b"WEBP"):
                                logger.error(
                                    f"文件不是有效的 WebP 格式 "
                                    f"(header: {header[:12].hex()})"
                                )
                                return

                        img = Image.open(webp_file_path)
                        # 转换 RGBA 到 RGB（PNG 不支持透明度）
                        if img.mode in ("RGBA", "LA", "P"):
                            background = Image.new("RGB", img.size, (255, 255, 255))
                            if img.mode == "P":
                                img = img.convert("RGBA")
                            background.paste(
                                img, mask=img.split()[-1] if img.mode == "RGBA" else None
                            )
                            img = background
                        img.save(png_file_path, "PNG")
                        logger.debug(f"贴纸已转换为 PNG: {png_file_path}")
                    except Exception as e:
                        logger.error(f"贴纸格式转换失败: {e}")
                        return

                    # 检测贴纸图片中的文字
                    result = await detector.detect_image(
                        image_path=png_file_path,
                        user_id=message.from_user.id,
                        chat_id=message.chat.id,
                    )

        # 处理视频 WebM 贴纸
        else:
            with managed_temp_file(suffix=".webm") as webm_file_path:
                # 下载视频贴纸
                await bot.download(sticker, destination=webm_file_path)
                logger.debug(
                    f"视频贴纸已下载: {webm_file_path}, "
                    f"大小: {sticker.width}x{sticker.height}, "
                    f"文件大小: {sticker.file_size} bytes"
                )

                # 提取首帧和中间帧进行检测（方案B）
                try:
                    # 读取所有帧
                    frames = list(iio.imiter(webm_file_path, plugin="pyav"))
                    total_frames = len(frames)
                    logger.debug(f"视频总帧数: {total_frames}")

                    if total_frames == 0:
                        logger.warning("视频无有效帧")
                        return

                    # 确定检测帧索引：1/3 帧 + 2/3 帧
                    check_indices = [total_frames // 3]  # 1/3 帧
                    if total_frames > 2:
                        check_indices.append(total_frames * 2 // 3)  # 2/3 帧

                    logger.debug(f"将检测第 {check_indices} 帧 (1/3 和 2/3 位置)")

                    # 循环检测每一帧
                    for frame_idx in check_indices:
                        frame = frames[frame_idx]
                        logger.debug(
                            f"检测第 {frame_idx} 帧 (共{total_frames}帧): shape={frame.shape}"
                        )

                        with managed_temp_file(suffix=".png") as png_file_path:
                            # 转换为 PIL Image 并保存
                            img = Image.fromarray(frame)
                            # 转换为 RGB（如果需要）
                            if img.mode != "RGB":
                                img = img.convert("RGB")
                            img.save(png_file_path, "PNG")
                            logger.debug(
                                f"第 {frame_idx} 帧已保存为 PNG: {png_file_path}"
                            )

                            # 检测当前帧中的文字
                            result = await detector.detect_image(
                                image_path=png_file_path,
                                user_id=message.from_user.id,
                                chat_id=message.chat.id,
                            )

                            # 如果检测到垃圾，立即停止检测
                            if result["is_spam"]:
                                logger.info(
                                    f"第 {frame_idx} 帧检测到垃圾，停止后续检测"
                                )
                                break

                except Exception as e:
                    logger.error(f"视频贴纸帧提取失败: {e}")
                    return

        # 如果检测到垃圾
        if result["is_spam"]:
            logger.warning(
                f"检测到贴纸垃圾信息 [群组:{message.chat.id}] "
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
                    reason=f"贴纸垃圾信息: {', '.join(result['reasons'])}",
                )

                if success:
                    # 缓存原始消息的 OCR 文本，用于管理员反馈
                    if "ocr_text" in result["details"]:
                        redis = get_redis()
                        text_cache_key = RedisKeys.spam_message_text(
                            message.chat.id, message.message_id
                        )
                        await redis.setex(text_cache_key, 3600, result["details"]["ocr_text"])

                    # 发送提示消息
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
                        f"🚫 检测到贴纸垃圾信息并已处理\n\n"
                        f"用户: {format_user_mention(message.from_user)}\n"
                        f"原因: {', '.join(result['reasons'])}\n"
                        f"置信度: {result['confidence']:.2%}\n"
                        f"处罚: 禁言 10 分钟",
                        reply_markup=keyboard,
                    )
                    await auto_delete_message(alert_msg)

                    # 记录反馈
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
                logger.error(f"处理贴纸垃圾消息失败: {e}")

    except Exception as e:
        logger.error(f"贴纸检测失败: {e}")


@router.callback_query(F.data.startswith("spam_feedback:"))
async def on_spam_feedback(callback: CallbackQuery) -> None:
    """处理管理员反馈

    ✅ P1-12: 从 Redis 缓存获取真实文本，而非使用 message_id
    """
    try:
        _, feedback_type, _user_id_str, message_id_str = callback.data.split(":", 3)

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
        text_cache_key = RedisKeys.spam_message_text(callback.message.chat.id, int(message_id_str))
        cached_text = await redis.get(text_cache_key)

        if cached_text:
            # 使用缓存的真实文本
            await detector.add_feedback(
                text=cached_text,
                is_spam=is_spam,
                labeled_by=callback.from_user.id,
            )
            logger.debug(
                f"使用缓存文本添加反馈 [消息ID:{message_id_str}] [长度:{len(cached_text)}]"
            )
        else:
            # 缓存已过期或不存在，记录警告但仍然接受反馈
            logger.warning(
                f"反馈文本缓存未命中 [消息ID:{message_id_str}]，" "可能是缓存过期或系统重启导致"
            )
            await callback.answer("⚠️ 原始文本已过期，反馈可能不完整", show_alert=True)
            return

        # 更新消息
        feedback_text = "✅ 确认为正常消息" if not is_spam else "❌ 确认为垃圾信息"
        await callback.message.edit_text(
            callback.message.text
            + f"\n\n{feedback_text} (by {format_user_mention(callback.from_user)})"
        )

        await callback.answer(f"反馈已记录: {feedback_text}")

        logger.info(
            f"管理员反馈 [管理员:{callback.from_user.id}] " f"类型: {'垃圾' if is_spam else '正常'}"
        )

    except Exception as e:
        logger.error(f"处理管理员反馈失败: {e}")
        await callback.answer("❌ 处理失败", show_alert=True)
