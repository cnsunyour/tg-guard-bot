"""群组用户清理命令处理器"""

from datetime import datetime

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.core.telethon_client import get_telethon_client
from src.core.utils import check_admin_permission
from src.services.cleanup import CleanupResult, CleanupService
from src.services.member_query import MemberQueryService


def escape_html(text: str) -> str:
    """转义 HTML 特殊字符"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


router = Router(name="cleanup")


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message, bot: Bot) -> Message | None:
    """群组用户清理命令

    用法：
      /cleanup                    - 预览清理（显示异常用户数量）
      /cleanup run                - 执行清理（所有异常用户）
      /cleanup deleted            - 仅清理已删除用户
      /cleanup restricted         - 仅清理受限用户（Telegram 官方限制）
      /cleanup scam               - 仅清理诈骗标记用户
      /cleanup fake               - 仅清理虚假标记用户
      /cleanup refresh            - 强制刷新成员缓存
      /cleanup cache              - 查看缓存状态

    注意：
      - restricted: Telegram 官方限制的垃圾/违规账号
      - scam: 被标记为诈骗的账号
      - fake: 被标记为虚假身份的账号
      - deleted: 已删除的账号
    """
    if not message.from_user or not message.chat:
        return None

    # 检查是否在群组中
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ 此命令只能在群组中使用")
        return None

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有群组管理员可以使用此命令")
        return None

    # 检查 Telethon 客户端
    telethon_client = get_telethon_client()
    if not telethon_client:
        await message.answer(
            "❌ Telethon 客户端未启用或未初始化\n\n请联系管理员配置 Telethon 并生成 session 文件"
        )
        return None

    # 解析参数
    if not message.text:
        return None

    args = message.text.split()
    subcommand = args[1].lower() if len(args) > 1 else "preview"

    member_query = MemberQueryService(telethon_client)

    try:
        # 刷新缓存
        if subcommand == "refresh":
            return await _handle_refresh(message, member_query)

        # 查看缓存
        if subcommand == "cache":
            return await _handle_cache_info(message, member_query)

        # 预览清理
        if subcommand == "preview" or len(args) == 1:
            return await _handle_preview(message, member_query)

        # 执行清理
        if subcommand == "run":
            return await _handle_run(message, bot, member_query)

        # 仅清理已删除用户
        if subcommand == "deleted":
            return await _handle_deleted(message, bot, member_query)

        # 仅清理受限用户
        if subcommand == "restricted":
            return await _handle_restricted(message, bot, member_query)

        # 仅清理诈骗标记用户
        if subcommand == "scam":
            return await _handle_scam(message, bot, member_query)

        # 仅清理虚假标记用户
        if subcommand == "fake":
            return await _handle_fake(message, bot, member_query)

        # 未知子命令
        await message.answer(
            "❌ 未知子命令\n\n"
            "<b>用法</b>:\n"
            "• /cleanup - 预览清理\n"
            "• /cleanup run - 执行清理（所有异常用户）\n"
            "• /cleanup deleted - 仅清理已删除用户\n"
            "• /cleanup restricted - 仅清理受限用户\n"
            "• /cleanup scam - 仅清理诈骗标记用户\n"
            "• /cleanup fake - 仅清理虚假标记用户\n"
            "• /cleanup refresh - 刷新缓存\n"
            "• /cleanup cache - 查看缓存状态"
        )
        return None

    except Exception as e:
        logger.error(f"清理命令执行失败: {e}")
        await message.answer(f"❌ 执行失败: {escape_html(str(e))}")
        return None


async def _handle_refresh(message: Message, member_query: MemberQueryService) -> Message | None:
    """处理刷新缓存"""
    if not message.chat:
        return None

    status_msg = await message.answer("🔄 正在刷新成员缓存...")

    try:
        count = await member_query.refresh_cache(message.chat.id)
        await status_msg.edit_text(f"✅ 缓存已刷新\n\n成员数量: {count}")
        return status_msg  # 返回消息对象以便中间件自动删除
    except Exception as e:
        logger.error(f"刷新缓存失败: {e}")
        await status_msg.edit_text(f"❌ 刷新失败: {escape_html(str(e))}")
        return status_msg  # 返回消息对象以便中间件自动删除


async def _handle_cache_info(message: Message, member_query: MemberQueryService) -> Message | None:
    """处理查看缓存信息"""
    if not message.chat:
        return None

    try:
        cache_info = await member_query.get_cache_info(message.chat.id)
        if not cache_info:
            return await message.answer("ℹ️ 缓存不存在，请先执行 /cleanup 或 /cleanup refresh")

        cached_at = datetime.fromisoformat(cache_info["cached_at"])
        ttl_minutes = cache_info["ttl_seconds"] // 60

        return await message.answer(
            f"📋 <b>缓存信息</b>\n\n"
            f"成员数量: {cache_info['member_count']}\n"
            f"缓存时间: {cached_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"剩余有效期: {ttl_minutes} 分钟"
        )
    except Exception as e:
        logger.error(f"获取缓存信息失败: {e}")
        return await message.answer(f"❌ 获取失败: {escape_html(str(e))}")


async def _handle_preview(message: Message, member_query: MemberQueryService) -> Message | None:
    """处理预览清理"""
    if not message.chat:
        return None

    status_msg = await message.answer("🔍 正在扫描群组成员...")

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)

        restricted_count = len(result["restricted"])
        scam_count = len(result["scam"])
        fake_count = len(result["fake"])
        deleted_count = len(result["deleted"])
        total = restricted_count + scam_count + fake_count + deleted_count

        if total == 0:
            await status_msg.edit_text("✅ 没有需要清理的异常用户")
            return status_msg

        await status_msg.edit_text(
            f"📊 <b>清理预览</b>\n\n"
            f"🚫 受限用户 (restricted): {restricted_count} 人\n"
            f"⚠️ 诈骗标记 (scam): {scam_count} 人\n"
            f"🤖 虚假标记 (fake): {fake_count} 人\n"
            f"❌ 已删除账号 (deleted): {deleted_count} 人\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"总计: {total} 人\n\n"
            f"执行清理请使用: /cleanup run"
        )
        return status_msg
    except Exception as e:
        logger.error(f"预览清理失败: {e}")
        await status_msg.edit_text(f"❌ 预览失败: {escape_html(str(e))}")
        return status_msg


async def _handle_run(
    message: Message, bot: Bot, member_query: MemberQueryService
) -> Message | None:
    """处理执行完整清理"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer("🔍 正在扫描群组成员...")

    try:
        # 获取待清理用户
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)

        restricted_users = result["restricted"]
        scam_users = result["scam"]
        fake_users = result["fake"]
        deleted_users = result["deleted"]
        total = len(restricted_users) + len(scam_users) + len(fake_users) + len(deleted_users)

        if total == 0:
            await status_msg.edit_text("✅ 没有需要清理的异常用户")
            return status_msg

        await status_msg.edit_text(
            f"🚀 开始清理 {total} 个异常用户...\n\n"
            f"🚫 受限用户: {len(restricted_users)} 人\n"
            f"⚠️ 诈骗标记: {len(scam_users)} 人\n"
            f"🤖 虚假标记: {len(fake_users)} 人\n"
            f"❌ 已删除账号: {len(deleted_users)} 人"
        )

        # 执行清理
        cleanup_result = CleanupResult()

        # 清理受限用户
        if restricted_users:
            restricted_result = await CleanupService.execute_cleanup(
                bot, message.chat.id, restricted_users, message.from_user.id, "restricted"
            )
            cleanup_result.restricted_kicked = restricted_result.restricted_kicked
            cleanup_result.restricted_failed = restricted_result.restricted_failed
            cleanup_result.errors.extend(restricted_result.errors)

        # 清理诈骗用户
        if scam_users:
            scam_result = await CleanupService.execute_cleanup(
                bot, message.chat.id, scam_users, message.from_user.id, "scam"
            )
            cleanup_result.scam_kicked = scam_result.scam_kicked
            cleanup_result.scam_failed = scam_result.scam_failed
            cleanup_result.errors.extend(scam_result.errors)

        # 清理虚假用户
        if fake_users:
            fake_result = await CleanupService.execute_cleanup(
                bot, message.chat.id, fake_users, message.from_user.id, "fake"
            )
            cleanup_result.fake_kicked = fake_result.fake_kicked
            cleanup_result.fake_failed = fake_result.fake_failed
            cleanup_result.errors.extend(fake_result.errors)

        # 清理已删除用户
        if deleted_users:
            deleted_result = await CleanupService.execute_cleanup(
                bot, message.chat.id, deleted_users, message.from_user.id, "deleted"
            )
            cleanup_result.deleted_kicked = deleted_result.deleted_kicked
            cleanup_result.deleted_failed = deleted_result.deleted_failed
            cleanup_result.errors.extend(deleted_result.errors)

        # 显示结果
        await _show_cleanup_result(status_msg, cleanup_result)
        return status_msg

    except Exception as e:
        logger.error(f"执行清理失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")
        return status_msg


async def _handle_deleted(
    message: Message, bot: Bot, member_query: MemberQueryService
) -> Message | None:
    """处理仅清理已删除用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer("🔍 正在扫描已删除用户...")

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        deleted_users = result["deleted"]

        if not deleted_users:
            await status_msg.edit_text("✅ 没有已删除用户")
            return status_msg

        await status_msg.edit_text(f"🚀 开始清理 {len(deleted_users)} 个已删除用户...")

        cleanup_result = await CleanupService.execute_cleanup(
            bot, message.chat.id, deleted_users, message.from_user.id, "deleted"
        )

        await _show_cleanup_result(status_msg, cleanup_result)
        return status_msg

    except Exception as e:
        logger.error(f"清理已删除用户失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")
        return status_msg


async def _handle_restricted(
    message: Message, bot: Bot, member_query: MemberQueryService
) -> Message | None:
    """处理仅清理受限用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer("🔍 正在扫描受限用户...")

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        restricted_users = result["restricted"]

        if not restricted_users:
            await status_msg.edit_text("✅ 没有受限用户")
            return status_msg

        await status_msg.edit_text(f"🚀 开始清理 {len(restricted_users)} 个受限用户...")

        cleanup_result = await CleanupService.execute_cleanup(
            bot, message.chat.id, restricted_users, message.from_user.id, "restricted"
        )

        await _show_cleanup_result(status_msg, cleanup_result)
        return status_msg

    except Exception as e:
        logger.error(f"清理受限用户失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")
        return status_msg


async def _handle_scam(
    message: Message, bot: Bot, member_query: MemberQueryService
) -> Message | None:
    """处理仅清理诈骗标记用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer("🔍 正在扫描诈骗标记用户...")

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        scam_users = result["scam"]

        if not scam_users:
            await status_msg.edit_text("✅ 没有诈骗标记用户")
            return status_msg

        await status_msg.edit_text(f"🚀 开始清理 {len(scam_users)} 个诈骗标记用户...")

        cleanup_result = await CleanupService.execute_cleanup(
            bot, message.chat.id, scam_users, message.from_user.id, "scam"
        )

        await _show_cleanup_result(status_msg, cleanup_result)
        return status_msg

    except Exception as e:
        logger.error(f"清理诈骗标记用户失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")
        return status_msg


async def _handle_fake(
    message: Message, bot: Bot, member_query: MemberQueryService
) -> Message | None:
    """处理仅清理虚假标记用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer("🔍 正在扫描虚假标记用户...")

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        fake_users = result["fake"]

        if not fake_users:
            await status_msg.edit_text("✅ 没有虚假标记用户")
            return status_msg

        await status_msg.edit_text(f"🚀 开始清理 {len(fake_users)} 个虚假标记用户...")

        cleanup_result = await CleanupService.execute_cleanup(
            bot, message.chat.id, fake_users, message.from_user.id, "fake"
        )

        await _show_cleanup_result(status_msg, cleanup_result)
        return status_msg

    except Exception as e:
        logger.error(f"清理虚假标记用户失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")
        return status_msg


async def _show_cleanup_result(message: Message, result) -> None:
    """显示清理结果"""
    total_kicked = (
        result.restricted_kicked + result.scam_kicked + result.fake_kicked + result.deleted_kicked
    )
    total_failed = (
        result.restricted_failed + result.scam_failed + result.fake_failed + result.deleted_failed
    )

    text = "✅ <b>清理完成</b>\n\n"
    text += f"🚫 受限用户: {result.restricted_kicked} 踢出, {result.restricted_failed} 失败\n"
    text += f"⚠️ 诈骗标记: {result.scam_kicked} 踢出, {result.scam_failed} 失败\n"
    text += f"🤖 虚假标记: {result.fake_kicked} 踢出, {result.fake_failed} 失败\n"
    text += f"❌ 已删除账号: {result.deleted_kicked} 踢出, {result.deleted_failed} 失败\n"
    text += f"\n总计: {total_kicked} 踢出, {total_failed} 失败"

    if result.errors:
        error_count = len(result.errors)
        text += f"\n\n⚠️ {error_count} 个错误（仅显示前 5 个）:"
        for error in result.errors[:5]:
            text += f"\n• {escape_html(error)}"

    await message.edit_text(text)
