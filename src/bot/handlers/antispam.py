"""反垃圾消息处理器"""

import contextlib
import json
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import imageio.v3 as iio
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger
from PIL import Image

from src.bot.handlers.antispam_render import (
    PunishmentKey,
    build_feedback_result,
    build_immediate_keyboard,
    build_immediate_processed,
    build_review_ban_result,
    build_review_false_positive_result,
    build_review_keyboard,
    build_review_prompt,
)
from src.core.cache import PermissionCache  # ✅ P1-10: 导入权限缓存
from src.core.config import settings
from src.core.i18n import get_resolver, get_translator
from src.core.redis import RedisKeys, get_redis  # ✅ P1-12: 导入 Redis 和键管理
from src.core.utils import (
    auto_delete_message,
    check_admin_permission,
    format_trusted_user_mention,
    format_user_mention,
    get_chat_administrators_mention,
    should_skip_sender,
)
from src.models.group import Group
from src.repositories.audit_repo import AuditRepository
from src.repositories.group_repo import GroupRepository
from src.services.activity import ActivityService  # 活跃度服务
from src.services.context_service import ContextService  # 上下文服务
from src.services.moderation import ModerationService
from src.services.spam_detector import get_detector
from src.services.spam_review import (
    SpamMessageType,
    SpamReviewState,
    consume_review_state,
    create_review_state,
    delete_review_state_if_match,
    get_review_state,
    review_lock,
)
from src.services.username_mapping import UsernameMappingService  # ✅ username 映射服务

router = Router(name="antispam")

# 已注册的命令集合（将在 bot 启动时自动从 dispatcher 中提取）
_registered_commands: set[str] = set()


async def update_username_mapping_if_needed(message: Message) -> None:
    """更新 username 映射（如果用户有 username）

    Args:
        message: 消息对象
    """
    if message.from_user and message.from_user.username:
        await UsernameMappingService.update_mapping(
            user_id=message.from_user.id,
            username=message.from_user.username,
        )


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


def is_channel_as_sender(message: Message) -> bool:
    """检查消息是否使用频道身份发送(频道马甲)

    当用户以频道身份发言时：
    - message.sender_chat 不为 None
    - message.sender_chat.id != message.chat.id (发送者是外部频道)
    - message.sender_chat.type == "channel"

    Args:
        message: 消息对象

    Returns:
        是否是频道马甲消息
    """
    if message.sender_chat is None:
        return False

    # 排除匿名管理员(sender_chat 是群组本身)
    if message.sender_chat.id == message.chat.id:
        return False

    # 检查是否是频道类型
    return message.sender_chat.type == "channel"


def is_external_forward(message: Message) -> bool:
    """检查消息是否是从外部群组/频道转发的

    规则：
    - 转发自其他群组/频道：算外部转发
    - 转发自本群内用户：不算外部转发
    - 转发自其他用户：不算外部转发

    Args:
        message: 消息对象

    Returns:
        是否是外部转发消息
    """
    # 没有转发信息，不是转发消息
    if not message.forward_origin:
        return False

    from aiogram.types import MessageOriginChannel, MessageOriginChat

    # 转发自频道：算外部转发
    if isinstance(message.forward_origin, MessageOriginChannel):
        return True

    # 转发自群组
    if isinstance(message.forward_origin, MessageOriginChat):
        # 如果是转发自本群，不算外部转发；转发自其他群组，算外部转发
        return message.forward_origin.sender_chat.id != message.chat.id

    # 其他情况（转发自用户、隐藏用户等）：不算外部转发
    return False


def has_url_entities(message: Message) -> bool:
    """检查消息是否包含 URL 链接

    Args:
        message: 消息对象

    Returns:
        是否包含 URL 链接
    """
    if not message.entities:
        return False

    from aiogram.enums import MessageEntityType

    # 检查是否有 URL 或文本链接实体
    for entity in message.entities:
        if entity.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK):
            return True

    return False


async def check_and_handle_channel_as_sender(message: Message, bot: Bot) -> bool:
    """检测并处理频道马甲消息(统一处理函数)

    Args:
        message: 消息对象
        bot: Bot 实例

    Returns:
        True 表示应跳过后续处理（频道马甲已处理，或关联频道消息），False 表示继续正常处理
    """
    # 类型缩小
    assert message.chat

    # 检查是否是频道类型发言
    if not is_channel_as_sender(message):
        return False

    # 快速路径：Telegram 系统服务账号（777000 关联频道同步转发）/ Bot 自身，直接跳过
    if message.from_user and should_skip_sender(message.from_user.id, bot.id):
        logger.debug(f"跳过特殊来源消息（系统账号/Bot自身）[群组:{message.chat.id}]")
        return True

    # 检查群组是否启用反频道马甲
    try:
        group = await GroupRepository.get(message.chat.id)
        if group and not group.anti_channel_enabled:
            logger.debug(f"群组 {message.chat.id} 未启用反频道马甲功能，跳过频道马甲检测")
            return False

        # 排除群组关联频道（linked channel）的消息（双重保险）
        try:
            chat_info = await bot.get_chat(message.chat.id)
            if (
                chat_info.linked_chat_id is not None
                and message.sender_chat is not None
                and message.sender_chat.id == chat_info.linked_chat_id
            ):
                logger.debug(
                    f"跳过群组关联频道消息 [群组:{message.chat.id}] "
                    f"[关联频道:{message.sender_chat.id}]"
                )
                return True
        except Exception as e:
            logger.debug(f"获取群组关联频道信息失败，继续马甲检测: {e}")

        # 频道马甲消息：删除消息并警告
        channel_title = (
            message.sender_chat.title
            if message.sender_chat and message.sender_chat.title
            else "未知频道"
        )
        sender_chat_id = message.sender_chat.id if message.sender_chat else 0
        logger.warning(
            f"检测到频道马甲消息 [群组:{message.chat.id}] [频道:{channel_title}({sender_chat_id})]"
        )

        # 删除消息
        with contextlib.suppress(Exception):
            await message.delete()

        # 发送警告通知(如果有实际用户)
        if message.from_user:
            user_mention = format_user_mention(message.from_user)
            warning_text = (
                f"⚠️ {user_mention}\n\n"
                f"检测到您使用频道身份 <b>{channel_title}</b> 发言。\n"
                f"本群禁止使用频道马甲发言，您的消息已被删除。\n\n"
                f"💡 请使用您的个人账号正常发言。"
            )

            # 发送警告并自动删除
            warning_msg = await message.answer(warning_text, parse_mode="HTML")
            await auto_delete_message(warning_msg, delay=30)

            # 记录警告到数据库
            moderation_service = ModerationService()
            await moderation_service.warn_user(
                bot=bot,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                operator_id=bot.id,
                reason="使用频道马甲发言",
            )
        else:
            # 没有实际用户信息，仅在群组发送提示
            warning_text = (
                f"⚠️ 检测到频道 <b>{channel_title}</b> 的消息。\n\n"
                f"本群禁止使用频道身份发言，该消息已被删除。"
            )
            warning_msg = await message.answer(warning_text, parse_mode="HTML")
            await auto_delete_message(warning_msg, delay=30)

        logger.info(
            f"已处理频道马甲消息 [群组:{message.chat.id}] [频道:{channel_title}({sender_chat_id})]"
        )
        return True

    except Exception as e:
        logger.error(f"处理频道马甲消息失败: {e}")
        return False


async def check_non_text_message(
    message: Message, bot: Bot, message_type: str, group_activity_enabled: bool
) -> bool:
    """检查非文本消息是否允许发送（活跃度检查）

    注意：
    - 调用此函数前应已过滤管理员
    - group_activity_enabled=True: 限制活跃度 <= 0 的用户
    - group_activity_enabled=False: 不限制，但仍记录活跃度
    - 活跃度记录始终执行，用于置信度修正、检测豁免等

    Args:
        message: 消息对象
        bot: Bot 实例
        message_type: 消息类型（"photo", "sticker", "video" 等）
        group_activity_enabled: 群组活跃度开关（来自 group.activity_enabled）

    Returns:
        True 表示消息已被阻止（调用者应直接 return），False 表示允许
    """
    # 类型缩小
    assert message.from_user

    # 跳过 Telegram 系统服务账号（777000 关联频道同步转发）和 Bot 自身
    if should_skip_sender(message.from_user.id, bot.id):
        return False

    # 检查活跃度是否允许发送非文本消息
    allowed, current_activity = await ActivityService.check_non_text_allowed(
        message.chat.id, message.from_user.id, check_enabled=group_activity_enabled
    )

    if not allowed:
        # 删除消息
        try:
            await message.delete()
            logger.info(
                f"活跃度限制：阻止非文本消息 [群组:{message.chat.id}] "
                f"[用户:{message.from_user.id}] [类型:{message_type}] [活跃度:{current_activity}]"
            )

            # 私聊通知用户
            await notify_activity_restriction(bot, message.from_user.id, current_activity)

        except Exception as e:
            logger.error(f"删除非文本消息失败: {e}")

        return True  # 消息已被阻止

    # 允许发送，记录活跃度
    await ActivityService.record_non_text_message(message.chat.id, message.from_user.id)
    return False  # 允许通过


async def notify_activity_restriction(bot: Bot, user_id: int, current_activity: int) -> None:
    """私聊通知用户活跃度限制

    Args:
        bot: Bot 实例
        user_id: 用户 ID
        current_activity: 当前活跃度
    """
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ **消息被限制**\n\n"
                f"您当前的活跃度为 **{current_activity}**，无法发送非文本消息。\n\n"
                "📝 **如何恢复:**\n"
                "发送文本消息可以增加活跃度，每条文本消息 +1 活跃度。\n\n"
                "💡 当活跃度 &gt; 0 时，即可发送图片、贴纸等非文本消息。"
            ),
            parse_mode="Markdown",
        )
        logger.debug(f"已私聊通知用户 {user_id} 活跃度限制")
    except Exception as e:
        # 用户可能未启动 Bot，这是正常情况
        logger.debug(f"通知用户活跃度限制失败（用户可能未启动 Bot）: {e}")


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


async def _handle_spam_with_review(
    message: Message,
    bot: Bot,
    result: dict[str, Any],
    *,
    message_type: SpamMessageType,
    recognized_text: str | None = None,
) -> None:
    """创建不可变复核快照并发送管理员审核提示。

    流程：
    1. 构造 ``SpamReviewState``，``create_review_state`` 以 ``SET NX EX`` 写入；键已
       存在（同消息重复 update / 编辑再次命中）则直接返回，保留首快照、不重复发提示。
    2. 按群 locale 渲染 prompt + 按钮，``message.answer`` 发送（不用 reply，避免回复
       预览泄露 spammer 显示名）。
    3. admin lookup / locale / 渲染 / 发送任一失败，则按 ``review_id`` CAS 删除刚写入
       的 state，避免遗留 24h 无法触达的 review（codex 3b-3 review P2）。
    """
    if not message.from_user:
        logger.warning("消息缺少发送者信息，跳过处理")
        return

    original_text = message.text or message.caption or ""
    state = SpamReviewState(
        offender_user_id=message.from_user.id,
        message_type=message_type,
        original_text=original_text,
        recognized_text=recognized_text,
        sample_text=recognized_text or original_text,
        reason_codes=tuple(str(reason) for reason in result["reasons"]),
        confidence=float(result["confidence"]),
    )
    created = await create_review_state(state, message.chat.id, message.message_id)
    if created is None:
        return  # 已有 review 快照，不覆盖、不重复发提示

    try:
        offender_mention = format_user_mention(message.from_user)
        admin_mentions = await get_chat_administrators_mention(bot, message.chat.id)
        group_locale = await get_resolver().for_group(message.chat.id)
        localizer = get_translator().for_locale(group_locale)
        prompt = build_review_prompt(localizer, state, offender_mention)
        header = f"🔔 {admin_mentions}\n\n" if admin_mentions else ""
        await message.answer(
            header + prompt,
            reply_markup=build_review_keyboard(localizer, message.message_id, state.review_id),
        )
    except Exception:
        # 准备或发送失败：清理刚写入的 state，避免遗留无法触达的 review
        await delete_review_state_if_match(message.chat.id, message.message_id, state.review_id)
        raise

    logger.info(
        f"垃圾消息待审核 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
        f"类型:{message_type.value} 置信度:{state.confidence:.2%}"
    )


async def _apply_immediate_punishment(
    message: Message,
    bot: Bot,
    result: dict[str, Any],
    *,
    message_type: SpamMessageType,
    recognized_text: str | None = None,
) -> None:
    """删除垃圾消息、立即处罚，并发送可反馈的本地化通知。

    提取自原 _handle_spam_immediately + sticker / edited 内联处罚：按置信度选
    ban_user_temporarily / mute_user，缓存样本文本供 spam_feedback，按群 locale
    渲染通知（含反馈按钮）+ auto_delete，入库正样本。
    """
    if not message.from_user:
        logger.warning("消息缺少发送者信息，跳过处理")
        return

    try:
        await message.delete()
        reasons = ", ".join(str(reason) for reason in result["reasons"])

        punishment_key: PunishmentKey
        if result["confidence"] >= settings.spam_high_confidence_threshold:
            success, error_msg = await ModerationService.ban_user_temporarily(
                bot=bot,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                operator_id=bot.id,
                duration=60,
                reason=f"垃圾信息（高置信度）: {reasons}",
            )
            punishment_key = "temporary_ban"
        else:
            success, error_msg = await ModerationService.mute_user(
                bot=bot,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                operator_id=bot.id,
                duration=10,
                reason=f"垃圾信息: {reasons}",
            )
            punishment_key = "mute"

        if not success:
            logger.error(f"处罚垃圾用户失败: {error_msg}")
            return

        sample_text = recognized_text or message.text or message.caption or ""
        await get_redis().setex(
            RedisKeys.spam_message_text(message.chat.id, message.message_id),
            86400,
            sample_text,
        )

        admin_mentions = await get_chat_administrators_mention(bot, message.chat.id)
        group_locale = await get_resolver().for_group(message.chat.id)
        localizer = get_translator().for_locale(group_locale)
        text = build_immediate_processed(
            localizer,
            message_type=message_type,
            offender_mention=format_user_mention(message.from_user),
            reason_codes=tuple(str(reason) for reason in result["reasons"]),
            confidence=result["confidence"],
            punishment_key=punishment_key,
            message_id=message.message_id,
        )
        header = f"🔔 {admin_mentions}\n\n" if admin_mentions else ""
        alert_msg = await message.answer(
            header + text,
            reply_markup=build_immediate_keyboard(
                localizer, message.from_user.id, message.message_id
            ),
        )
        await auto_delete_message(alert_msg)

        detector = get_detector()
        await detector.add_feedback(
            text=sample_text,
            is_spam=True,
            labeled_by=bot.id,
            confidence=result["confidence"],
        )
    except Exception as e:
        logger.error(f"处理垃圾消息失败: {e}")


async def _route_spam_detection(
    message: Message,
    bot: Bot,
    result: dict[str, Any],
    group: Group | None,
    *,
    message_type: SpamMessageType,
    recognized_text: str | None = None,
) -> None:
    """按群组确认模式配置分发垃圾消息（确认→review，否则→立即处罚）。"""
    if group and group.spam_confirm_enabled:
        await _handle_spam_with_review(
            message,
            bot,
            result,
            message_type=message_type,
            recognized_text=recognized_text,
        )
    else:
        await _apply_immediate_punishment(
            message,
            bot,
            result,
            message_type=message_type,
            recognized_text=recognized_text,
        )


@router.message(Command("antispam"))
async def cmd_antispam(message: Message, bot: Bot) -> None:
    """反垃圾配置命令"""
    # 类型缩小
    assert message.from_user
    assert message.chat

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
            [
                InlineKeyboardButton(
                    text="🔍 管理员确认模式",
                    callback_data=f"antispam_confirm_menu:{message.chat.id}",
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
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        _, chat_id_str, action = callback.data.split(":")
        chat_id = int(chat_id_str)

        # ✅ 权限验证
        if callback.from_user.id not in settings.admin_ids:
            # ✅ P1-10: 使用 Redis 缓存减少 API 调用
            if not await PermissionCache.is_admin(callback.bot, chat_id, callback.from_user.id):  # type: ignore[arg-type]
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
        await message.edit_text(f"✅ 反垃圾功能{status}")
        await callback.answer(f"反垃圾{status}")

        logger.info(f"群组 {chat_id} 反垃圾功能{status}")

    except Exception as e:
        logger.error(f"切换反垃圾失败: {e}")
        await callback.answer("❌ 操作失败", show_alert=True)


@router.callback_query(F.data.startswith("antispam_stats:"))
async def on_antispam_stats(callback: CallbackQuery) -> None:
    """查看反垃圾统计"""
    try:
        # 类型检查
        if not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

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

        await message.edit_text(text)
        await callback.answer("统计信息已更新")

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        await callback.answer("❌ 获取失败", show_alert=True)


@router.callback_query(F.data.startswith("antispam_retrain:"))
async def on_antispam_retrain(callback: CallbackQuery) -> None:
    """重新训练模型"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        _, chat_id_str = callback.data.split(":")
        int(chat_id_str)

        # ✅ 权限验证 - 重训练是敏感操作，仅超级管理员可执行
        if callback.from_user.id not in settings.admin_ids:
            await callback.answer("❌ 只有超级管理员可以重新训练模型", show_alert=True)
            logger.warning(f"用户 {callback.from_user.id} 尝试触发模型重训练但无权限")
            return

        await callback.answer("正在训练模型，请稍候...")

        detector = get_detector()
        success, message_text = await detector.retrain_model()

        if success:
            await message.edit_text(f"✅ {message_text}")
        else:
            await message.edit_text(f"❌ {message_text}")

    except Exception as e:
        logger.error(f"重新训练模型失败: {e}")
        await callback.answer("❌ 训练失败", show_alert=True)


@router.callback_query(F.data.startswith("antispam_confirm_menu:"))
async def on_antispam_confirm_menu(callback: CallbackQuery) -> None:
    """显示管理员确认模式子菜单"""
    # 类型检查
    if not callback.data or not callback.message:
        await callback.answer("❌ 数据错误", show_alert=True)
        return

    from aiogram.types import InaccessibleMessage, Message

    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer("❌ 消息不可访问", show_alert=True)
        return

    message: Message = callback.message

    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)

    # 权限验证
    if callback.from_user.id not in settings.admin_ids:
        if not callback.bot:
            await callback.answer("❌ Bot 实例不可用", show_alert=True)
            return
        if not await PermissionCache.is_admin(callback.bot, chat_id, callback.from_user.id):
            await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
            return

    # 获取当前配置
    group = await GroupRepository.get_or_create(chat_id, message.chat.title or "")
    status = "✅ 已启用" if group.spam_confirm_enabled else "❌ 已关闭"

    # 显示子菜单
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 启用确认模式", callback_data=f"antispam_confirm_toggle:{chat_id}:on"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ 关闭确认模式", callback_data=f"antispam_confirm_toggle:{chat_id}:off"
                )
            ],
            [InlineKeyboardButton(text="🔙 返回", callback_data=f"antispam_back:{chat_id}")],
        ]
    )

    await message.edit_text(
        f"🔍 <b>管理员确认模式</b>\n\n"
        f"当前状态: {status}\n\n"
        f"<b>功能说明:</b>\n"
        f"• 启用后，检测到垃圾消息时不会立即处罚\n"
        f"• 发送确认提示，等待管理员确认后再处理\n"
        f"• 降低误判对用户的影响\n\n"
        f"<b>注意:</b>\n"
        f"• 确认模式下原消息会保留，让管理员查看完整内容\n"
        f"• 管理员确认为垃圾后，统一踢出并永久封禁\n"
        f"• 确认为误判后，消息保留并入库负样本",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("antispam_confirm_toggle:"))
async def on_antispam_confirm_toggle(callback: CallbackQuery) -> None:
    """处理管理员确认模式开关"""
    # 类型检查
    if not callback.data or not callback.message:
        await callback.answer("❌ 数据错误", show_alert=True)
        return

    from aiogram.types import InaccessibleMessage, Message

    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer("❌ 消息不可访问", show_alert=True)
        return

    message: Message = callback.message

    _, chat_id_str, action = callback.data.split(":")
    chat_id = int(chat_id_str)

    # 权限验证
    if callback.from_user.id not in settings.admin_ids:
        if not callback.bot:
            await callback.answer("❌ Bot 实例不可用", show_alert=True)
            return
        if not await PermissionCache.is_admin(callback.bot, chat_id, callback.from_user.id):
            await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
            return

    # 参数白名单验证
    if action not in ["on", "off"]:
        await callback.answer("❌ 无效的操作", show_alert=True)
        return

    enabled = action == "on"

    # 更新配置
    success = await GroupRepository.update_spam_confirm_settings(chat_id, enabled)

    if success:
        status = "✅ 已启用" if enabled else "❌ 已关闭"
        await callback.answer(f"✅ 管理员确认模式{status}", show_alert=True)

        # 更新菜单显示
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ 启用确认模式",
                        callback_data=f"antispam_confirm_toggle:{chat_id}:on",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ 关闭确认模式",
                        callback_data=f"antispam_confirm_toggle:{chat_id}:off",
                    )
                ],
                [InlineKeyboardButton(text="🔙 返回", callback_data=f"antispam_back:{chat_id}")],
            ]
        )

        await message.edit_text(
            f"🔍 <b>管理员确认模式</b>\n\n"
            f"当前状态: {status}\n\n"
            f"<b>功能说明:</b>\n"
            f"• 启用后，检测到垃圾消息时不会立即处罚\n"
            f"• 发送确认提示，等待管理员确认后再处理\n"
            f"• 降低误判对用户的影响\n\n"
            f"<b>注意:</b>\n"
            f"• 确认模式下原消息会保留，让管理员查看完整内容\n"
            f"• 管理员确认为垃圾后，统一踢出并永久封禁\n"
            f"• 确认为误判后，消息保留并入库负样本",
            reply_markup=keyboard,
        )

        logger.info(f"群组 {chat_id} 管理员确认模式已{'启用' if enabled else '关闭'}")
    else:
        await callback.answer("❌ 更新失败", show_alert=True)


@router.callback_query(F.data.startswith("antispam_back:"))
async def on_antispam_back(callback: CallbackQuery) -> None:
    """返回反垃圾主菜单"""
    # 类型检查
    if not callback.data or not callback.message:
        await callback.answer("❌ 数据错误", show_alert=True)
        return

    from aiogram.types import InaccessibleMessage, Message

    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer("❌ 消息不可访问", show_alert=True)
        return

    message: Message = callback.message

    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)

    # 恢复主菜单
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 启用反垃圾", callback_data=f"antispam_toggle:{chat_id}:on"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ 禁用反垃圾", callback_data=f"antispam_toggle:{chat_id}:off"
                )
            ],
            [InlineKeyboardButton(text="📊 查看统计", callback_data=f"antispam_stats:{chat_id}")],
            [
                InlineKeyboardButton(
                    text="🔄 重新训练模型", callback_data=f"antispam_retrain:{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 管理员确认模式", callback_data=f"antispam_confirm_menu:{chat_id}"
                )
            ],
        ]
    )

    await message.edit_text("🛡️ 反垃圾配置", reply_markup=keyboard)
    await callback.answer()


@router.message(Command("antichannel"))
async def cmd_antichannel(message: Message, bot: Bot) -> None:
    """反频道马甲配置命令"""
    # 类型缩小
    assert message.from_user
    assert message.chat

    logger.debug(f"收到 /antichannel 命令 [群组:{message.chat.id}] [用户:{message.from_user.id}]")

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

    logger.debug("权限检查通过，准备查询当前配置")

    # 获取当前配置
    try:
        group = await GroupRepository.get(message.chat.id)
        current_status = "✅ 已启用" if (group and group.anti_channel_enabled) else "❌ 已禁用"
    except Exception as e:
        logger.error(f"获取群组配置失败: {e}")
        current_status = "✅ 已启用(默认)"

    # 显示配置菜单
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 启用反频道马甲",
                    callback_data=f"antichannel_toggle:{message.chat.id}:on",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ 禁用反频道马甲",
                    callback_data=f"antichannel_toggle:{message.chat.id}:off",
                )
            ],
        ]
    )

    logger.debug("发送配置菜单消息")
    reply = await message.answer(
        f"🎭 <b>反频道马甲配置</b>\n\n"
        f"当前状态: {current_status}\n\n"
        f"💡 <b>说明</b>：\n"
        f"• 启用后，禁止用户以频道身份发言\n"
        f"• 频道马甲消息会被删除，并记录警告\n"
        f"• 有助于减少广告和频道宣传",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    logger.debug(f"配置菜单已发送，消息ID: {reply.message_id}")
    await auto_delete_message(reply)


@router.callback_query(F.data.startswith("antichannel_toggle:"))
async def on_antichannel_toggle(callback: CallbackQuery) -> None:
    """处理反频道马甲开关"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        _, chat_id_str, action = callback.data.split(":")
        chat_id = int(chat_id_str)

        # ✅ 权限验证
        if callback.from_user.id not in settings.admin_ids:
            # ✅ P1-10: 使用 Redis 缓存减少 API 调用
            if not await PermissionCache.is_admin(callback.bot, chat_id, callback.from_user.id):  # type: ignore[arg-type]
                await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
                logger.warning(
                    f"用户 {callback.from_user.id} 尝试修改群组 {chat_id} 反频道马甲设置但无权限"
                )
                return

        # ✅ 参数白名单验证
        if action not in ["on", "off"]:
            await callback.answer("❌ 无效的操作", show_alert=True)
            logger.warning(f"收到无效的反频道马甲开关操作: {action}")
            return

        # 更新配置
        enabled = action == "on"
        group = await GroupRepository.get_or_create(chat_id)
        group.anti_channel_enabled = enabled
        await GroupRepository.update_antichannel_settings(chat_id, enabled)

        status_text = "✅ 已启用" if enabled else "❌ 已禁用"
        await callback.answer(f"反频道马甲功能 {status_text}", show_alert=False)

        # 更新消息
        await message.edit_text(
            f"🎭 <b>反频道马甲配置</b>\n\n"
            f"当前状态: {status_text}\n\n"
            f"💡 <b>说明</b>：\n"
            f"• 启用后，禁止用户以频道身份发言\n"
            f"• 频道马甲消息会被删除，并记录警告\n"
            f"• 有助于减少广告和频道宣传",
            parse_mode="HTML",
        )

        logger.info(f"群组 {chat_id} 反频道马甲功能已{status_text}")

    except Exception as e:
        logger.error(f"处理反频道马甲开关失败: {e}")
        await callback.answer("❌ 操作失败", show_alert=True)


@router.message(F.text)
async def on_message(message: Message, bot: Bot) -> None:
    """处理所有文本消息，检测垃圾"""
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过已注册的命令消息
    if message.text and message.text.startswith("/"):
        # 提取命令名（格式：/command 或 /command@botname 或 /command args）
        command_match = re.match(r"^/([a-zA-Z][a-zA-Z0-9_]*)(@\w+)?(\s|$)", message.text or "")
        if command_match:
            command_name = command_match.group(1)
            # 只跳过已注册的命令
            if command_name in _registered_commands:
                logger.debug(
                    f"[文本处理器] 跳过已注册命令 [群组:{message.chat.id}] [命令:{command_name}]"
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

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user，这里提前返回
    if not message.from_user:
        logger.debug(f"消息没有 from_user 信息，跳过后续处理 [群组:{message.chat.id}]")
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    # 跳过超级管理员消息
    if message.from_user.id in settings.admin_ids:
        logger.debug(f"跳过超级管理员文本消息 [用户:{message.from_user.id}]")
        # ✅ 记录管理员消息到上下文（有助于 AI 理解群组讨论主题）
        await ContextService.record_message(message)
        return

    # ✅ P1-10: 使用 Redis 缓存减少 API 调用
    # 跳过群组管理员消息
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        logger.debug(
            f"跳过群组管理员文本消息 [群组:{message.chat.id}] [用户:{message.from_user.id}]"
        )
        # ✅ 记录管理员消息到上下文
        await ContextService.record_message(message)
        return

    # 检查群组是否启用反垃圾
    try:
        group = await GroupRepository.get(message.chat.id)
        if group and not group.antispam_enabled:
            return
    except Exception as e:
        # ✅ L6: 添加日志，不静默吞掉异常
        logger.debug(f"检查管理员权限失败（非关键）: {e}")
        group = None  # 设置默认值，避免 UnboundLocalError

    # 检查是否是外部转发或带链接的消息（需要活跃度支撑）
    is_special_message = is_external_forward(message) or has_url_entities(message)

    # 记录活跃度（管理员已在上面跳过，不会记录）
    activity = None
    if is_special_message:
        # 外部转发/带链接消息：按非文本消息处理
        if await check_non_text_message(
            message,
            bot,
            "forward" if is_external_forward(message) else "link",
            group.activity_enabled if group else True,
        ):
            return  # 活跃度不足，消息已被删除
        # 活跃度足够，已扣除，继续垃圾检测
        activity = await ActivityService.get_activity(message.chat.id, message.from_user.id)
    else:
        # 普通文本消息：增加活跃度（+1）
        activity = await ActivityService.record_text_message(message.chat.id, message.from_user.id)

    # ✅ 活跃度跳过检测：高活跃度用户直接信任
    if activity is not None:
        global_threshold = settings.activity_skip_spam_check_threshold

        # 确定最终阈值（全局配置优先）
        if global_threshold > 0:
            # 全局阈值 > 0：使用全局配置
            final_threshold = global_threshold
            threshold_source = "全局配置"
        elif global_threshold == 0:
            # 全局阈值 = 0：使用群组配置
            final_threshold = group.activity_skip_threshold if group else 0
            threshold_source = "群组配置"
        else:
            # 全局阈值 < 0：全局禁用
            final_threshold = 0
            threshold_source = "全局禁用"

        if final_threshold > 0 and activity >= final_threshold:
            logger.debug(
                f"跳过垃圾检测 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
                f"[活跃度:{activity}] [阈值:{final_threshold}] [来源:{threshold_source}]"
            )
            # ✅ 记录高活跃度用户消息到上下文
            await ContextService.record_message(message)
            return  # 直接返回，不进行垃圾检测

    # 获取检测器
    detector = get_detector()

    # ✅ 获取上下文（如果启用）
    context_text = None
    context_messages_raw = None
    if settings.context_enabled and settings.ai_spam_enabled:
        try:
            context = await ContextService.get_conversation_context(message)
            context_text = ContextService.format_context_for_ai(
                context,
                message.text or "",
                message.message_id,
                chat_title=message.chat.title,
                chat_description=message.chat.description,
            )
            # 转换为 dict 列表（给 Embedding 用）
            context_messages_raw = [dict(msg) for msg in context["recent_messages"]]
            logger.debug(
                f"已构建上下文 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
                f"[回复链:{len(context['reply_chain'])}] [最近消息:{len(context['recent_messages'])}]"
            )
        except Exception as e:
            logger.warning(f"获取上下文失败，使用普通检测: {e}")
            context_text = None
            context_messages_raw = None

    # ✅ 检查是否启用确认模式，决定是否跳过 AI 自动入库
    skip_auto_train = bool(group and group.spam_confirm_enabled)

    # 检测垃圾（传入活跃度、上下文和消息对象，使用并行 AI 检测）
    result = await detector.detect_with_ai_context(
        text=message.text or "",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        activity=activity,
        context_text=context_text,
        context_messages=context_messages_raw,
        message=message,
        skip_auto_train=skip_auto_train,  # ✅ 确认模式下跳过 AI 自动入库
    )

    # 如果检测到垃圾
    if result["is_spam"]:
        logger.warning(
            f"检测到垃圾信息 [群组:{message.chat.id}] "
            f"[用户:{message.from_user.id}] "
            f"阶段: {result['stage']}, "
            f"置信度: {result['confidence']:.2f}, "
            f"原因: {', '.join(map(str, result['reasons']))}"
        )

        # 根据群组配置决定处理方式
        await _route_spam_detection(
            message,
            bot,
            dict(result),  # type: ignore[arg-type]
            group,
            message_type=SpamMessageType.text,
        )
    else:
        # ✅ 消息通过检测，记录到上下文缓存（防止污染上下文）
        await ContextService.record_message(message)


@router.message(F.photo)
async def on_photo_message(message: Message, bot: Bot) -> None:
    """处理图片消息，检测垃圾"""
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过没有发送者的消息
    if not message.from_user:
        return

    # 跳过管理员消息
    if message.from_user.id in settings.admin_ids:
        return

    # ✅ P1-10: 使用 Redis 缓存减少 API 调用
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 获取群组配置
    group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

    # 检查反垃圾是否启用
    if not group.antispam_enabled:
        return

    # ✅ 处理频道马甲（优先级最高）
    if await check_and_handle_channel_as_sender(message, bot):
        return  # 已处理频道马甲，直接返回

    # ✅ 活跃度系统：检查是否允许发送非文本消息
    # ✅ 活跃度系统：检查是否允许发送非文本消息
    if await check_non_text_message(message, bot, "photo", group.activity_enabled):
        return  # 活跃度不足，消息已被删除

    # ✅ 活跃度跳过检测：高活跃度用户直接信任（activity 变量后续也用于 Vision 置信度调整）
    activity = await ActivityService.get_activity(message.chat.id, message.from_user.id)
    global_threshold = settings.activity_skip_spam_check_threshold

    # 确定最终阈值（全局配置优先）
    if global_threshold > 0:
        final_threshold = global_threshold
        threshold_source = "全局配置"
    elif global_threshold == 0:
        final_threshold = group.activity_skip_threshold if group else 0
        threshold_source = "群组配置"
    else:
        final_threshold = 0
        threshold_source = "全局禁用"

    if final_threshold > 0 and activity >= final_threshold:
        logger.debug(
            f"跳过图片垃圾检测 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
            f"[活跃度:{activity}] [阈值:{final_threshold}] [来源:{threshold_source}]"
        )
        # 记录到上下文
        await ContextService.record_message(message)
        return  # 直接返回，不进行垃圾检测

    # 更新 username 映射
    await update_username_mapping_if_needed(message)

    # 获取检测器
    detector = get_detector()

    # 下载图片到临时文件
    if not message.photo:
        logger.warning("图片消息缺少 photo 数据")
        return

    # ✅ 构建 Vision 直判需要的上下文：caption + 群组对话上下文
    caption = (message.caption or "").strip() or None
    context_text: str | None = None
    if settings.context_enabled and (settings.ai_spam_enabled or settings.ai_spam_vision_enabled):
        try:
            context = await ContextService.get_conversation_context(message)
            context_text = ContextService.format_context_for_ai(
                context,
                caption or "[图片消息]",
                message.message_id,
                chat_title=message.chat.title,
                chat_description=message.chat.description,
            )
        except Exception as e:
            logger.warning(f"获取图片消息上下文失败，使用无上下文检测: {e}")
            context_text = None

    # 确认模式下跳过自动训练入库，避免管理员审批前先落库
    skip_auto_train = bool(group and group.spam_confirm_enabled)

    with managed_temp_file(suffix=".jpg") as temp_file_path:
        photo = message.photo[-1]  # 获取最大尺寸的图片
        logger.debug(f"开始下载图片 [file_id:{photo.file_id}]")
        await bot.download(photo, destination=temp_file_path)
        logger.debug(f"图片已下载到临时文件: {temp_file_path}")

        # 检测图片（Vision 直判优先，失败降级 OCR）
        result = await detector.detect_image(
            image_path=temp_file_path,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            caption=caption,
            context_text=context_text,
            activity=activity,
            skip_auto_train=skip_auto_train,
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

        # 获取识别出的文本（如果有）
        recognized_text = result.get("details", {}).get("recognized_text", "")

        await _route_spam_detection(
            message,
            bot,
            dict(result),  # type: ignore[arg-type]
            group,
            message_type=SpamMessageType.photo,
            recognized_text=recognized_text,
        )


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

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user，这里提前返回
    if not message.from_user:
        logger.debug(f"消息没有 from_user 信息，跳过后续处理 [群组:{message.chat.id}]")
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

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

    # 检查群组配置
    try:
        group = await GroupRepository.get(message.chat.id)
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # ✅ 活跃度检查（管理员已在上面跳过）
    if await check_non_text_message(
        message, bot, "sticker", group.activity_enabled if group else True
    ):
        return  # 消息已被删除

    # ✅ 活跃度跳过检测：高活跃度用户直接信任
    # activity 变量在后续 detect_image 也会用到，提升作用域
    activity = await ActivityService.get_activity(message.chat.id, message.from_user.id)
    global_threshold = settings.activity_skip_spam_check_threshold

    # 确定最终阈值（全局配置优先）
    if global_threshold > 0:
        final_threshold = global_threshold
        threshold_source = "全局配置"
    elif global_threshold == 0:
        final_threshold = group.activity_skip_threshold if group else 0
        threshold_source = "群组配置"
    else:
        final_threshold = 0
        threshold_source = "全局禁用"

    if final_threshold > 0 and activity >= final_threshold:
        logger.debug(
            f"跳过贴纸垃圾检测 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
            f"[活跃度:{activity}] [阈值:{final_threshold}] [来源:{threshold_source}]"
        )
        # 记录到上下文
        await ContextService.record_message(message)
        return  # 直接返回，不进行垃圾检测

    # 检查群组是否启用反垃圾
    if group and not group.antispam_enabled:
        return

    # 获取检测器
    detector = get_detector()

    # 检查 Vision 是否可用（多模态未配置则跳过贴纸检测）
    if not detector.ai_detector.vision_enabled:
        logger.debug("Vision 不可用，跳过贴纸检测")
        return

    # ✅ 构建贴纸 caption（emoji + 贴纸包名）和群组对话上下文
    sticker_meta_parts: list[str] = ["Telegram 贴纸"]
    if message.sticker:
        if message.sticker.emoji:
            sticker_meta_parts.append(f"Emoji: {message.sticker.emoji}")
        if message.sticker.set_name:
            sticker_meta_parts.append(f"贴纸包: {message.sticker.set_name}")
    sticker_caption = "\n".join(sticker_meta_parts)

    sticker_context_text: str | None = None
    if settings.context_enabled and (settings.ai_spam_enabled or settings.ai_spam_vision_enabled):
        try:
            sticker_context = await ContextService.get_conversation_context(message)
            sticker_context_text = ContextService.format_context_for_ai(
                sticker_context,
                sticker_caption,
                message.message_id,
                chat_title=message.chat.title,
                chat_description=message.chat.description,
            )
        except Exception as e:
            logger.warning(f"获取贴纸消息上下文失败，使用无上下文检测: {e}")
            sticker_context_text = None

    sticker_skip_auto_train = bool(group and group.spam_confirm_enabled)

    # PIL 各工厂返回不同具体类型（ImageFile/Image），函数级统一按基类声明
    img: Image.Image

    # 使用 context manager 确保临时文件清理
    try:
        # 检查 sticker 是否存在
        if not message.sticker:
            return

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

            # ✅ 安全修复：防止 Gzip 炸弹攻击 - 先检查压缩文件大小
            MAX_COMPRESSED_SIZE = 256 * 1024  # 256KB 压缩文件上限
            if sticker.file_size and sticker.file_size > MAX_COMPRESSED_SIZE:
                logger.warning(
                    f"TGS 文件过大: {sticker.file_size} bytes (限制: {MAX_COMPRESSED_SIZE})"
                )
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

                    # ✅ 安全修复：使用流式解压防止 Gzip 炸弹攻击
                    MAX_DECOMPRESSED_SIZE = 10 * 1024 * 1024  # 10MB 解压后大小限制
                    import zlib

                    decompressed_data = b""
                    with open(tgs_path, "rb") as f:
                        # 创建 gzip 解压器（16 + MAX_WBITS 表示 gzip 格式）
                        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        chunk_size = 65536  # 64KB 块

                        try:
                            while True:
                                chunk = f.read(chunk_size)
                                if not chunk:
                                    break

                                # 解压当前块
                                decompressed_chunk = decompressor.decompress(
                                    chunk, max_length=MAX_DECOMPRESSED_SIZE - len(decompressed_data)
                                )
                                decompressed_data += decompressed_chunk

                                # 边解压边检查大小
                                if len(decompressed_data) >= MAX_DECOMPRESSED_SIZE:
                                    logger.warning(
                                        f"TGS 文件解压后过大，停止解压 (限制: {MAX_DECOMPRESSED_SIZE} bytes)"
                                    )
                                    return

                            # 处理剩余数据
                            decompressed_data += decompressor.flush()
                        except zlib.error as e:
                            logger.error(f"TGS 文件解压失败: {e}")
                            return

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
                    frame_1_3 = int(ip) + total_frames // 3  # 1/3 位置的绝对帧号
                    check_indices = [frame_1_3]

                    if total_frames > 2:
                        frame_2_3 = int(ip) + total_frames * 2 // 3  # 2/3 位置的绝对帧号
                        check_indices.append(frame_2_3)

                    logger.debug(
                        f"将检测第 {check_indices} 帧 "
                        f"(相对位置: 1/3={total_frames // 3}, 2/3={total_frames * 2 // 3}; "
                        f"帧范围: {int(ip)}-{int(op)})"
                    )

                    # 导入 TGS 动画
                    anim = import_tgs(str(tgs_file_path))

                    # 循环检测每一帧
                    for frame_idx in check_indices:
                        relative_pos = frame_idx - int(ip)
                        logger.debug(
                            f"渲染第 {frame_idx} 帧 "
                            f"(相对位置: {relative_pos}/{total_frames}, 进度: {relative_pos / total_frames:.1%})"
                        )

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
                                        background.paste(img, mask=img.split()[-1])  # alpha channel
                                    else:
                                        background.paste(img)
                                    background.save(png_file_path, "PNG")
                                elif img.mode != "RGB":
                                    # 其他模式直接转 RGB
                                    img.convert("RGB").save(png_file_path, "PNG")

                            # 检测当前帧（Vision 直判优先，失败降级 OCR）
                            result = await detector.detect_image(
                                image_path=png_file_path,
                                user_id=message.from_user.id,
                                chat_id=message.chat.id,
                                caption=sticker_caption,
                                context_text=sticker_context_text,
                                activity=activity,
                                skip_auto_train=sticker_skip_auto_train,
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
                                    f"文件不是有效的 WebP 格式 (header: {header[:12].hex()})"
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

                    # 检测贴纸图片（Vision 直判优先，失败降级 OCR）
                    result = await detector.detect_image(
                        image_path=png_file_path,
                        user_id=message.from_user.id,
                        chat_id=message.chat.id,
                        caption=sticker_caption,
                        context_text=sticker_context_text,
                        activity=activity,
                        skip_auto_train=sticker_skip_auto_train,
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
                    frame_1_3 = total_frames // 3
                    check_indices = [frame_1_3]

                    if total_frames > 2:
                        frame_2_3 = total_frames * 2 // 3
                        check_indices.append(frame_2_3)

                    logger.debug(
                        f"将检测第 {check_indices} 帧 (1/3 和 2/3 位置, 总帧数: {total_frames})"
                    )

                    # 循环检测每一帧
                    for frame_idx in check_indices:
                        frame = frames[frame_idx]
                        logger.debug(
                            f"检测第 {frame_idx} 帧 "
                            f"(进度: {frame_idx}/{total_frames}={frame_idx / total_frames:.1%}, shape={frame.shape})"
                        )

                        with managed_temp_file(suffix=".png") as png_file_path:
                            # 转换为 PIL Image 并保存
                            img = Image.fromarray(frame)
                            # 转换为 RGB（如果需要）
                            if img.mode != "RGB":
                                img = img.convert("RGB")
                            img.save(png_file_path, "PNG")
                            logger.debug(f"第 {frame_idx} 帧已保存为 PNG: {png_file_path}")

                            # 检测当前帧（Vision 直判优先，失败降级 OCR）
                            result = await detector.detect_image(
                                image_path=png_file_path,
                                user_id=message.from_user.id,
                                chat_id=message.chat.id,
                                caption=sticker_caption,
                                context_text=sticker_context_text,
                                activity=activity,
                                skip_auto_train=sticker_skip_auto_train,
                            )

                            # 如果检测到垃圾，立即停止检测
                            if result["is_spam"]:
                                logger.info(f"第 {frame_idx} 帧检测到垃圾，停止后续检测")
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

            await _route_spam_detection(
                message,
                bot,
                dict(result),  # type: ignore[arg-type]
                group,
                message_type=SpamMessageType.sticker,
                recognized_text=result.get("details", {}).get("recognized_text"),
            )

    except Exception as e:
        logger.error(f"贴纸检测失败: {e}")


@router.message(F.video)
async def on_video_message(message: Message, bot: Bot) -> None:
    """处理视频消息（活跃度检查）"""
    if message.chat.type == "private":
        return

    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user
    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    if message.from_user.id in settings.admin_ids:
        return

    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组配置
    try:
        group = await GroupRepository.get(message.chat.id)
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # 活跃度检查
    if await check_non_text_message(
        message, bot, "video", group.activity_enabled if group else True
    ):
        return


@router.message(F.animation)
async def on_animation_message(message: Message, bot: Bot) -> None:
    """处理 GIF 动画消息（活跃度检查）"""
    if message.chat.type == "private":
        return

    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user
    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    if message.from_user.id in settings.admin_ids:
        return

    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组配置
    try:
        group = await GroupRepository.get(message.chat.id)
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # 活跃度检查
    if await check_non_text_message(
        message, bot, "animation", group.activity_enabled if group else True
    ):
        return


@router.message(F.voice)
async def on_voice_message(message: Message, bot: Bot) -> None:
    """处理语音消息（活跃度检查）"""
    if message.chat.type == "private":
        return

    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user
    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    if message.from_user.id in settings.admin_ids:
        return

    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组配置
    try:
        group = await GroupRepository.get(message.chat.id)
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # 活跃度检查
    if await check_non_text_message(
        message, bot, "voice", group.activity_enabled if group else True
    ):
        return


@router.message(F.video_note)
async def on_video_note_message(message: Message, bot: Bot) -> None:
    """处理视频笔记消息（活跃度检查）"""
    if message.chat.type == "private":
        return

    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user
    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    if message.from_user.id in settings.admin_ids:
        return

    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组配置
    try:
        group = await GroupRepository.get(message.chat.id)
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # 活跃度检查
    if await check_non_text_message(
        message, bot, "video_note", group.activity_enabled if group else True
    ):
        return


@router.message(F.document)
async def on_document_message(message: Message, bot: Bot) -> None:
    """处理文件消息（活跃度检查）"""
    if message.chat.type == "private":
        return

    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user
    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    if message.from_user.id in settings.admin_ids:
        return

    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组配置
    try:
        group = await GroupRepository.get(message.chat.id)
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # 活跃度检查
    if await check_non_text_message(
        message, bot, "document", group.activity_enabled if group else True
    ):
        return


@router.message(F.audio)
async def on_audio_message(message: Message, bot: Bot) -> None:
    """处理音频消息（活跃度检查）"""
    if message.chat.type == "private":
        return

    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    # 频道马甲消息可能没有 from_user，后续逻辑需要 from_user
    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    if message.from_user.id in settings.admin_ids:
        return

    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组配置
    try:
        group = await GroupRepository.get(message.chat.id)
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # 活跃度检查
    if await check_non_text_message(
        message, bot, "audio", group.activity_enabled if group else True
    ):
        return


@router.edited_message(F.text)
async def on_edited_text_message(message: Message, bot: Bot) -> None:
    """处理编辑后的文本消息，检测垃圾

    场景：垃圾发送者先发普通消息，然后编辑成垃圾信息
    """
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过已注册的命令消息
    if message.text and message.text.startswith("/"):
        command_match = re.match(r"^/([a-zA-Z][a-zA-Z0-9_]*)(@\w+)?(\s|$)", message.text or "")
        if command_match:
            command_name = command_match.group(1)
            if command_name in _registered_commands:
                return

    # 跳过匿名管理员消息
    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    # 跳过超级管理员消息
    if message.from_user.id in settings.admin_ids:
        return

    # 跳过群组管理员消息
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组是否启用反垃圾
    try:
        group = await GroupRepository.get(message.chat.id)
        if group and not group.antispam_enabled:
            return
    except Exception as e:
        logger.debug(f"检查管理员权限失败（非关键）: {e}")
        group = None

    # 注意：编辑消息不记录活跃度，因为原始消息已经记录过了
    # 直接进行垃圾检测

    # 获取活跃度（用于降低检测阈值）
    activity = await ActivityService.get_activity(message.chat.id, message.from_user.id)

    # ✅ 活跃度跳过检测：高活跃度用户直接信任
    if activity is not None:
        global_threshold = settings.activity_skip_spam_check_threshold

        if global_threshold > 0:
            final_threshold = global_threshold
            threshold_source = "全局配置"
        elif global_threshold == 0:
            final_threshold = group.activity_skip_threshold if group else 0
            threshold_source = "群组配置"
        else:
            final_threshold = 0
            threshold_source = "全局禁用"

        if final_threshold > 0 and activity >= final_threshold:
            logger.debug(
                f"跳过编辑消息垃圾检测 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
                f"[活跃度:{activity}] [阈值:{final_threshold}] [来源:{threshold_source}]"
            )
            return

    # 获取检测器
    detector = get_detector()

    # ✅ 获取上下文（如果启用）
    context_text = None
    if settings.context_enabled and settings.ai_spam_enabled:
        try:
            context = await ContextService.get_conversation_context(message)
            context_text = ContextService.format_context_for_ai(
                context,
                message.text or "",
                message.message_id,
                chat_title=message.chat.title,
                chat_description=message.chat.description,
            )
            logger.debug(
                f"已构建编辑消息上下文 [群组:{message.chat.id}] [用户:{message.from_user.id}] "
                f"[回复链:{len(context['reply_chain'])}] [最近消息:{len(context['recent_messages'])}]"
            )
        except Exception as e:
            logger.warning(f"获取编辑消息上下文失败，使用普通检测: {e}")
            context_text = None

    # 确认模式下跳过 AI 自动入库（避免 review 前持久化正样本，与误判负样本冲突）
    skip_auto_train = bool(group and group.spam_confirm_enabled)
    # 检测垃圾（传入活跃度和上下文）
    result = await detector.detect_with_ai_context(
        text=message.text or "",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        activity=activity,
        context_text=context_text,
        skip_auto_train=skip_auto_train,
    )

    # 如果检测到垃圾
    if result["is_spam"]:
        logger.warning(
            f"检测到编辑消息为垃圾信息 [群组:{message.chat.id}] "
            f"[用户:{message.from_user.id}] "
            f"阶段: {result['stage']}, "
            f"置信度: {result['confidence']:.2f}, "
            f"原因: {', '.join(map(str, result['reasons']))}"
        )

        await _route_spam_detection(
            message,
            bot,
            dict(result),  # type: ignore[arg-type]
            group,
            message_type=SpamMessageType.edited_text,
        )


@router.edited_message(F.photo)
async def on_edited_photo_message(message: Message, bot: Bot) -> None:
    """处理编辑后的图片消息（检测 caption 中的垃圾文字）

    注意：Telegram 不允许更换图片，只能编辑 caption
    """
    # 跳过私聊消息
    if message.chat.type == "private":
        return

    # 跳过匿名管理员消息
    if is_anonymous_admin(message):
        return

    # ✅ 检测并处理频道马甲消息
    if await check_and_handle_channel_as_sender(message, bot):
        return

    if not message.from_user:
        return

    # ✅ 更新 username 映射
    await update_username_mapping_if_needed(message)

    # 跳过超级管理员消息
    if message.from_user.id in settings.admin_ids:
        return

    # 跳过群组管理员消息
    if await PermissionCache.is_admin(bot, message.chat.id, message.from_user.id):
        return

    # 检查群组是否启用反垃圾
    try:
        group = await GroupRepository.get(message.chat.id)
        if group and not group.antispam_enabled:
            return
    except Exception as e:
        logger.debug(f"获取群组配置失败（非关键）: {e}")
        group = None

    # 如果有 caption，检测 caption 文字
    if message.caption:
        # 获取活跃度（用于降低检测阈值）
        activity = await ActivityService.get_activity(message.chat.id, message.from_user.id)

        # 检测器
        detector = get_detector()

        # ✅ 获取上下文（如果启用）
        context_text = None
        if settings.context_enabled and settings.ai_spam_enabled:
            try:
                context = await ContextService.get_conversation_context(message)
                context_text = ContextService.format_context_for_ai(
                    context,
                    message.caption,
                    message.message_id,
                    chat_title=message.chat.title,
                    chat_description=message.chat.description,
                )
                logger.debug(
                    f"已构建编辑图片 caption 上下文 [群组:{message.chat.id}] [用户:{message.from_user.id}]"
                )
            except Exception as e:
                logger.warning(f"获取编辑图片 caption 上下文失败: {e}")
                context_text = None

        # 确认模式下跳过 AI 自动入库（避免 review 前持久化正样本，与误判负样本冲突）
        skip_auto_train = bool(group and group.spam_confirm_enabled)
        # 检测 caption 中的垃圾文字
        result = await detector.detect_with_ai_context(
            text=message.caption,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            activity=activity,
            context_text=context_text,
            skip_auto_train=skip_auto_train,
        )

        if result["is_spam"]:
            logger.warning(
                f"检测到编辑图片 caption 为垃圾信息 [群组:{message.chat.id}] "
                f"[用户:{message.from_user.id}] "
                f"置信度: {result['confidence']:.2f}"
            )

            await _route_spam_detection(
                message,
                bot,
                dict(result),  # type: ignore[arg-type]
                group,
                message_type=SpamMessageType.edited_photo,
                recognized_text=result.get("details", {}).get("recognized_text"),
            )


async def _answer_toast(
    callback: CallbackQuery,
    key: str,
    *,
    show_alert: bool = True,
    **variables: object,
) -> None:
    """以点击者的个人语言偏好显示 callback toast（非群 locale）。"""
    locale = await get_resolver().for_user(callback.from_user.id)
    localizer = get_translator().for_locale(locale)
    await callback.answer(localizer.t(key, **variables), show_alert=show_alert)


@router.callback_query(F.data.startswith("spam_review:"))
async def on_spam_review_callback(callback: CallbackQuery, bot: Bot) -> None:
    """消费按原消息 ID + review_id 绑定的人工复核状态。

    callback_data 格式: ``spam_review:{ban|false_positive}:{orig_msg_id}:{review_id}``。
    先校验格式与权限并立即 answer processing（防 API/封禁/DB 致 callback 超时），
    再在 review_lock 内按 review_id 比较快照身份（防旧 prompt 消费被重建的新 state，
    codex 3b-2 review P2）。
    """
    if (
        not callback.data
        or not callback.message
        or isinstance(callback.message, InaccessibleMessage)
    ):
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
        return

    message: Message = callback.message
    try:
        prefix, action, orig_msg_id_raw, review_id = callback.data.split(":", 3)
        orig_msg_id = int(orig_msg_id_raw)
    except ValueError:
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
        return

    if (
        prefix != "spam_review"
        or action not in {"ban", "false_positive"}
        or orig_msg_id <= 0
        or re.fullmatch(r"[0-9a-fA-F]{16}", review_id) is None
    ):
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
        return

    if callback.from_user.id not in settings.admin_ids:
        if not await PermissionCache.is_admin(bot, message.chat.id, callback.from_user.id):
            await _answer_toast(callback, "antispam.callback.permission_denied.toast")
            return

    # 先回应防 callback 超时（processing 不弹框，轻量提示）
    await _answer_toast(callback, "antispam.callback.processing.toast", show_alert=False)

    async with review_lock(message.chat.id, orig_msg_id) as acquired:
        if not acquired:
            return  # 已有处理中

        state = await get_review_state(message.chat.id, orig_msg_id)
        if state is None or state.review_id != review_id:
            # state 不存在或被重建（review_id 不匹配）→ 旧按钮失效。不再 callback.answer
            # （已 answer processing，Telegram 仅允许一次），直接删旧提示清理
            with contextlib.suppress(Exception):
                await message.delete()
            return

        group_locale = await get_resolver().for_group(message.chat.id)
        localizer = get_translator().for_locale(group_locale)
        operator_mention = format_trusted_user_mention(callback.from_user)

        if action == "ban":
            success, error_msg = await ModerationService.ban_user(
                bot=bot,
                chat_id=message.chat.id,
                user_id=state.offender_user_id,
                operator_id=callback.from_user.id,
                reason="垃圾信息（管理员确认）",
                allow_left=True,
            )
            if not success:
                # 处罚失败：追加报错到原提示（保留证据 + 按钮），不 callback.answer
                # （已 answer processing）。codex review P2：勿替换整个 prompt 丢证据
                with contextlib.suppress(Exception):
                    await message.edit_text(
                        f"{message.text or ''}\n\n"
                        + localizer.t(
                            "antispam.review.action_failed.message",
                            error=error_msg or "",
                        ),
                        reply_markup=message.reply_markup,
                    )
                return

            detector = get_detector()
            await detector.add_feedback(
                text=state.sample_text,
                is_spam=True,
                labeled_by=callback.from_user.id,
            )
            await AuditRepository.log_action(
                group_id=message.chat.id,
                operator_id=callback.from_user.id,
                action="spam_review_ban",
                target_user_id=state.offender_user_id,
                details={
                    "orig_msg_id": orig_msg_id,
                    "text_preview": state.sample_text[:100],
                },
            )
            with contextlib.suppress(Exception):
                await bot.delete_message(message.chat.id, orig_msg_id)
            await consume_review_state(message.chat.id, orig_msg_id, review_id)
            with contextlib.suppress(Exception):
                await message.edit_text(
                    f"{message.text or ''}\n\n"
                    + build_review_ban_result(localizer, operator_mention, "permanent_ban"),
                    reply_markup=None,
                )
            await auto_delete_message(message, delay=30)
            logger.info(
                f"管理员确认垃圾消息 [群组:{message.chat.id}] "
                f"[用户:{state.offender_user_id}] [操作者:{callback.from_user.id}]"
            )
            return

        # false_positive：保留原消息 + 入库负样本
        detector = get_detector()
        await detector.add_feedback(
            text=state.sample_text,
            is_spam=False,
            labeled_by=callback.from_user.id,
        )
        await AuditRepository.log_action(
            group_id=message.chat.id,
            operator_id=callback.from_user.id,
            action="spam_review_false_positive",
            target_user_id=state.offender_user_id,
            details={
                "orig_msg_id": orig_msg_id,
                "text_preview": state.sample_text[:100],
            },
        )
        await consume_review_state(message.chat.id, orig_msg_id, review_id)
        with contextlib.suppress(Exception):
            await message.edit_text(
                f"{message.text or ''}\n\n"
                + build_review_false_positive_result(localizer, operator_mention),
                reply_markup=None,
            )
        await auto_delete_message(message, delay=30)
        logger.info(
            f"管理员确认误判 [群组:{message.chat.id}] "
            f"[用户:{state.offender_user_id}] [操作者:{callback.from_user.id}]"
        )


@router.callback_query(F.data.startswith("spam_confirm:"))
async def on_spam_confirm_callback(callback: CallbackQuery, bot: Bot) -> None:
    """旧版 spam_confirm 按钮失效处理（tombstone）。

    旧格式按钮（部署前发出的提示）点下去只提示"请重新触发检测"并 best-effort
    删除旧提示消息（消除 reply 预览），不解析旧参数、不读原消息、不处罚。
    删除前校验管理员权限，防非管理员 dismiss 待处理 alert（codex 3b-3 review P2）。
    """
    if not callback.message or isinstance(callback.message, InaccessibleMessage):
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
        return

    if callback.from_user.id not in settings.admin_ids:
        if not await PermissionCache.is_admin(bot, callback.message.chat.id, callback.from_user.id):
            await _answer_toast(callback, "antispam.callback.permission_denied.toast")
            return

    await _answer_toast(callback, "antispam.review.legacy.toast")
    with contextlib.suppress(Exception):
        await callback.message.delete()


@router.callback_query(F.data.startswith("spam_feedback:"))
async def on_spam_feedback(callback: CallbackQuery) -> None:
    """处理管理员反馈（立即处罚后的事后纠正）。

    业务逻辑（缓存文本取值 / 误判删旧正样本 / 确认垃圾替换 AI 样本 / 误判 unmute /
    自动训练）保留，仅文案 i18n（3b-4）。成功 toast 用简短 recorded，消息结果走
    build_feedback_result（群 locale）。
    """
    try:
        if not callback.data or not callback.message:
            await _answer_toast(callback, "antispam.callback.invalid_data.toast")
            return

        if not callback.bot:
            await _answer_toast(callback, "antispam.callback.invalid_data.toast")
            return

        if isinstance(callback.message, InaccessibleMessage):
            await _answer_toast(callback, "antispam.callback.invalid_data.toast")
            return

        message: Message = callback.message

        _, feedback_type, _user_id_str, message_id_str = callback.data.split(":", 3)
        user_id = int(_user_id_str)

        # 检查是否是管理员
        if callback.from_user.id not in settings.admin_ids:
            # ✅ P1-10: 使用 Redis 缓存减少 API 调用
            if not await PermissionCache.is_admin(
                callback.bot, message.chat.id, callback.from_user.id
            ):
                await _answer_toast(callback, "antispam.callback.permission_denied.toast")
                return

        detector = get_detector()

        # 根据反馈类型更新样本
        is_spam = feedback_type == "spam"

        # ✅ P1-12: 从 Redis 获取缓存的原始消息文本
        redis = get_redis()
        text_cache_key = RedisKeys.spam_message_text(message.chat.id, int(message_id_str))
        cached_text = await redis.get(text_cache_key)

        if cached_text:
            # ✅ 误判反馈：需要先删除之前的正样本记录，再添加负样本
            if not is_spam:
                from src.repositories.spam_repo import SpamRepository

                existing_sample = await SpamRepository.find_sample_by_text(
                    cached_text, is_spam=True
                )

                if existing_sample:
                    deleted = await SpamRepository.delete_sample(existing_sample.id)
                    if deleted:
                        logger.info(
                            f"误判反馈：已删除之前的正样本记录 [样本ID:{existing_sample.id}] "
                            f"[文本长度:{len(cached_text)}]"
                        )
            else:
                # ✅ 确认垃圾反馈：检查是否已存在 AI 自动入库的样本，避免重复
                from src.repositories.spam_repo import SpamRepository

                existing_sample = await SpamRepository.find_sample_by_text(
                    cached_text, is_spam=True
                )

                if existing_sample and existing_sample.labeled_by == -1:
                    deleted = await SpamRepository.delete_sample(existing_sample.id)
                    if deleted:
                        logger.info(
                            f"确认垃圾反馈：已删除 AI 自动入库的样本 [样本ID:{existing_sample.id}] "
                            f"[文本长度:{len(cached_text)}]，将替换为管理员标注"
                        )

            # 添加新的样本记录
            await detector.add_feedback(
                text=cached_text,
                is_spam=is_spam,
                labeled_by=callback.from_user.id,
            )
            logger.debug(
                f"使用缓存文本添加反馈 [消息ID:{message_id_str}] [长度:{len(cached_text)}]"
            )

            # ✅ 误判反馈：自动恢复用户权限
            if not is_spam:
                success = await ModerationService.unmute_user(
                    bot=callback.bot,
                    chat_id=message.chat.id,
                    user_id=user_id,
                    operator_id=callback.from_user.id,
                )
                if success:
                    logger.info(
                        f"误判反馈：已自动恢复用户 {user_id} 的权限 [群组:{message.chat.id}]"
                    )
                else:
                    logger.warning(
                        f"误判反馈：恢复用户 {user_id} 权限失败 [群组:{message.chat.id}]"
                    )

            # 检查是否需要自动训练
            try:
                triggered, train_message = await detector.check_and_auto_train(
                    admin_ids=settings.admin_ids
                )
                if triggered:
                    logger.info(f"反馈添加后触发自动训练: {train_message}")
            except Exception as e:
                logger.error(f"检查自动训练失败: {e}")
        else:
            # 缓存已过期或不存在，记录警告但仍然接受反馈
            logger.warning(
                f"反馈文本缓存未命中 [消息ID:{message_id_str}]，可能是缓存过期或系统重启导致"
            )
            await _answer_toast(callback, "antispam.feedback.expired.toast")
            return

        # 更新消息（群 locale 渲染结果段 + 移除按钮）
        group_locale = await get_resolver().for_group(message.chat.id)
        localizer = get_translator().for_locale(group_locale)
        operator_mention = format_trusted_user_mention(callback.from_user)
        feedback_result = build_feedback_result(localizer, is_spam, operator_mention)
        await message.edit_text(
            f"{message.text or ''}\n\n{feedback_result}",
            reply_markup=None,
        )

        # 自动删除提示消息
        await auto_delete_message(message, delay=30)

        # 成功 toast（简短，不弹框；edit_text 已展示完整结果）
        await _answer_toast(callback, "antispam.feedback.recorded.toast", show_alert=False)

        logger.info(
            f"管理员反馈 [管理员:{callback.from_user.id}] 类型: {'垃圾' if is_spam else '正常'}"
        )

    except Exception as e:
        logger.error(f"处理管理员反馈失败: {e}")
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
