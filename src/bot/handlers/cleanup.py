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
async def cmd_cleanup(message: Message, bot: Bot) -> None:
    """群组用户清理命令

    用法：
      /cleanup                    - 预览清理（显示待清理用户数量）
      /cleanup run                - 执行清理（已删除 + 很久不上线）
      /cleanup deleted            - 仅清理已删除用户
      /cleanup inactive           - 仅清理很久不上线的用户
      /cleanup inactive last_month - 清理一个月以上不上线的用户
      /cleanup refresh            - 强制刷新成员缓存
      /cleanup cache              - 查看缓存状态
    """
    if not message.from_user or not message.chat:
        return

    # 检查是否在群组中
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有群组管理员可以使用此命令")
        return

    # 检查 Telethon 客户端
    telethon_client = get_telethon_client()
    if not telethon_client:
        await message.answer(
            "❌ Telethon 客户端未启用或未初始化\n\n请联系管理员配置 Telethon 并生成 session 文件"
        )
        return

    # 解析参数
    if not message.text:
        return

    args = message.text.split()
    subcommand = args[1].lower() if len(args) > 1 else "preview"

    member_query = MemberQueryService(telethon_client)

    try:
        # 刷新缓存
        if subcommand == "refresh":
            await _handle_refresh(message, member_query)
            return

        # 查看缓存
        if subcommand == "cache":
            await _handle_cache_info(message, member_query)
            return

        # 预览清理
        if subcommand == "preview" or len(args) == 1:
            await _handle_preview(message, member_query)
            return

        # 执行清理
        if subcommand == "run":
            await _handle_run(message, bot, member_query)
            return

        # 仅清理已删除用户
        if subcommand == "deleted":
            await _handle_deleted(message, bot, member_query)
            return

        # 清理不活跃用户
        if subcommand == "inactive":
            inactive_status = args[2] if len(args) > 2 else "long_time_ago"
            if inactive_status not in ["long_time_ago", "last_month", "last_week"]:
                await message.answer(
                    "❌ 无效的不活跃状态\n\n可用选项: long_time_ago, last_month, last_week"
                )
                return
            await _handle_inactive(message, bot, member_query, inactive_status)
            return

        # 未知子命令
        await message.answer(
            "❌ 未知子命令\n\n"
            "<b>用法</b>:\n"
            "• /cleanup - 预览清理\n"
            "• /cleanup run - 执行清理\n"
            "• /cleanup deleted - 仅清理已删除用户\n"
            "• /cleanup inactive - 仅清理不活跃用户\n"
            "• /cleanup refresh - 刷新缓存\n"
            "• /cleanup cache - 查看缓存状态"
        )

    except Exception as e:
        logger.error(f"清理命令执行失败: {e}")
        await message.answer(f"❌ 执行失败: {escape_html(str(e))}")


async def _handle_refresh(message: Message, member_query: MemberQueryService) -> None:
    """处理刷新缓存"""
    if not message.chat:
        return

    status_msg = await message.answer("🔄 正在刷新成员缓存...")

    try:
        count = await member_query.refresh_cache(message.chat.id)
        await status_msg.edit_text(f"✅ 缓存已刷新\n\n成员数量: {count}")
    except Exception as e:
        logger.error(f"刷新缓存失败: {e}")
        await status_msg.edit_text(f"❌ 刷新失败: {escape_html(str(e))}")


async def _handle_cache_info(message: Message, member_query: MemberQueryService) -> None:
    """处理查看缓存信息"""
    if not message.chat:
        return

    try:
        cache_info = await member_query.get_cache_info(message.chat.id)
        if not cache_info:
            await message.answer("ℹ️ 缓存不存在，请先执行 /cleanup 或 /cleanup refresh")
            return

        cached_at = datetime.fromisoformat(cache_info["cached_at"])
        ttl_minutes = cache_info["ttl_seconds"] // 60

        await message.answer(
            f"📋 <b>缓存信息</b>\n\n"
            f"成员数量: {cache_info['member_count']}\n"
            f"缓存时间: {cached_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"剩余有效期: {ttl_minutes} 分钟"
        )
    except Exception as e:
        logger.error(f"获取缓存信息失败: {e}")
        await message.answer(f"❌ 获取失败: {escape_html(str(e))}")


async def _handle_preview(message: Message, member_query: MemberQueryService) -> None:
    """处理预览清理"""
    if not message.chat:
        return

    status_msg = await message.answer("🔍 正在扫描群组成员...")

    try:
        result = await CleanupService.preview_cleanup(
            member_query, message.chat.id, cleanup_deleted=True, cleanup_inactive=True
        )

        deleted_count = len(result["deleted"])
        inactive_count = len(result["inactive"])
        total = deleted_count + inactive_count

        if total == 0:
            await status_msg.edit_text("✅ 没有需要清理的用户")
            return

        await status_msg.edit_text(
            f"📊 <b>清理预览</b>\n\n"
            f"已删除用户: {deleted_count} 人\n"
            f"很久不上线: {inactive_count} 人\n"
            f"总计: {total} 人\n\n"
            f"执行清理请使用: /cleanup run"
        )
    except Exception as e:
        logger.error(f"预览清理失败: {e}")
        await status_msg.edit_text(f"❌ 预览失败: {escape_html(str(e))}")


async def _handle_run(message: Message, bot: Bot, member_query: MemberQueryService) -> None:
    """处理执行完整清理"""
    if not message.chat or not message.from_user:
        return

    status_msg = await message.answer("🔍 正在扫描群组成员...")

    try:
        # 获取待清理用户
        result = await CleanupService.preview_cleanup(
            member_query, message.chat.id, cleanup_deleted=True, cleanup_inactive=True
        )

        deleted_users = result["deleted"]
        inactive_users = result["inactive"]
        total = len(deleted_users) + len(inactive_users)

        if total == 0:
            await status_msg.edit_text("✅ 没有需要清理的用户")
            return

        await status_msg.edit_text(
            f"🚀 开始清理 {total} 个用户...\n\n"
            f"已删除用户: {len(deleted_users)} 人\n"
            f"很久不上线: {len(inactive_users)} 人"
        )

        # 执行清理
        cleanup_result = CleanupResult()

        # 清理已删除用户
        if deleted_users:
            deleted_result = await CleanupService.execute_cleanup(
                bot, message.chat.id, deleted_users, message.from_user.id, "已删除用户"
            )
            cleanup_result.deleted_kicked = deleted_result.deleted_kicked
            cleanup_result.deleted_failed = deleted_result.deleted_failed
            cleanup_result.errors.extend(deleted_result.errors)

        # 清理不活跃用户
        if inactive_users:
            inactive_result = await CleanupService.execute_cleanup(
                bot,
                message.chat.id,
                inactive_users,
                message.from_user.id,
                "很久不上线",
            )
            cleanup_result.inactive_kicked = inactive_result.inactive_kicked
            cleanup_result.inactive_failed = inactive_result.inactive_failed
            cleanup_result.errors.extend(inactive_result.errors)

        # 显示结果
        await _show_cleanup_result(status_msg, cleanup_result)

    except Exception as e:
        logger.error(f"执行清理失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")


async def _handle_deleted(message: Message, bot: Bot, member_query: MemberQueryService) -> None:
    """处理仅清理已删除用户"""
    if not message.chat or not message.from_user:
        return

    status_msg = await message.answer("🔍 正在扫描已删除用户...")

    try:
        deleted_users = await member_query.get_deleted_users(message.chat.id)

        if not deleted_users:
            await status_msg.edit_text("✅ 没有已删除用户")
            return

        await status_msg.edit_text(f"🚀 开始清理 {len(deleted_users)} 个已删除用户...")

        result = await CleanupService.execute_cleanup(
            bot, message.chat.id, deleted_users, message.from_user.id, "已删除用户"
        )

        await _show_cleanup_result(status_msg, result)

    except Exception as e:
        logger.error(f"清理已删除用户失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")


async def _handle_inactive(
    message: Message,
    bot: Bot,
    member_query: MemberQueryService,
    inactive_status: str,
) -> None:
    """处理清理不活跃用户"""
    if not message.chat or not message.from_user:
        return

    status_name = {
        "long_time_ago": "很久不上线",
        "last_month": "一个月以上不上线",
        "last_week": "一周以上不上线",
    }.get(inactive_status, inactive_status)

    status_msg = await message.answer(f"🔍 正在扫描{status_name}的用户...")

    try:
        inactive_users = await member_query.get_inactive_users(message.chat.id, inactive_status)

        if not inactive_users:
            await status_msg.edit_text(f"✅ 没有{status_name}的用户")
            return

        await status_msg.edit_text(f"🚀 开始清理 {len(inactive_users)} 个{status_name}的用户...")

        result = await CleanupService.execute_cleanup(
            bot, message.chat.id, inactive_users, message.from_user.id, status_name
        )

        await _show_cleanup_result(status_msg, result)

    except Exception as e:
        logger.error(f"清理不活跃用户失败: {e}")
        await status_msg.edit_text(f"❌ 执行失败: {escape_html(str(e))}")


async def _show_cleanup_result(message: Message, result) -> None:
    """显示清理结果"""
    total_kicked = result.deleted_kicked + result.inactive_kicked
    total_failed = result.deleted_failed + result.inactive_failed

    text = "✅ <b>清理完成</b>\n\n"
    text += f"已删除用户: {result.deleted_kicked} 踢出, {result.deleted_failed} 失败\n"
    text += f"不活跃用户: {result.inactive_kicked} 踢出, {result.inactive_failed} 失败\n"
    text += f"\n总计: {total_kicked} 踢出, {total_failed} 失败"

    if result.errors:
        error_count = len(result.errors)
        text += f"\n\n⚠️ {error_count} 个错误（仅显示前 5 个）:"
        for error in result.errors[:5]:
            text += f"\n• {escape_html(error)}"

    await message.edit_text(text)
