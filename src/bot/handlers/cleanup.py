"""群组用户清理命令处理器"""

from datetime import datetime

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.core.i18n import BoundLocalizer
from src.core.telethon_client import get_telethon_client
from src.core.utils import check_admin_permission, escape_html
from src.services.cleanup import (
    CleanupError,
    CleanupReason,
    CleanupResult,
    CleanupService,
)
from src.services.member_query import MemberQueryService

router = Router(name="cleanup")


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message, bot: Bot, localizer: BoundLocalizer) -> Message | None:
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
    """
    if not message.from_user or not message.chat:
        return None

    # 检查是否在群组中
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer(localizer.t("cleanup.error.group_only.message"))
        return None

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer(localizer.t("cleanup.error.admin_only.message"))
        return None

    # 检查 Telethon 客户端
    telethon_client = get_telethon_client()
    if not telethon_client:
        await message.answer(localizer.t("cleanup.error.telethon_unavailable.message"))
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
            return await _handle_refresh(message, member_query, localizer)

        # 查看缓存
        if subcommand == "cache":
            return await _handle_cache_info(message, member_query, localizer)

        # 预览清理
        if subcommand == "preview" or len(args) == 1:
            return await _handle_preview(message, member_query, localizer)

        # 执行清理
        if subcommand == "run":
            return await _handle_run(message, bot, member_query, localizer)

        # 仅清理已删除用户
        if subcommand == "deleted":
            return await _handle_deleted(message, bot, member_query, localizer)

        # 仅清理受限用户
        if subcommand == "restricted":
            return await _handle_restricted(message, bot, member_query, localizer)

        # 仅清理诈骗标记用户
        if subcommand == "scam":
            return await _handle_scam(message, bot, member_query, localizer)

        # 仅清理虚假标记用户
        if subcommand == "fake":
            return await _handle_fake(message, bot, member_query, localizer)

        # 未知子命令
        await message.answer(localizer.t("cleanup.usage.unknown_subcommand.message"))
        return None

    except Exception as e:
        logger.error(f"清理命令执行失败: {e}")
        await message.answer(
            localizer.t("cleanup.error.execution_failed.message", error=escape_html(str(e)))
        )
        return None


async def _handle_refresh(
    message: Message, member_query: MemberQueryService, localizer: BoundLocalizer
) -> Message | None:
    """处理刷新缓存"""
    if not message.chat:
        return None

    status_msg = await message.answer(localizer.t("cleanup.refresh.refreshing.message"))

    try:
        count = await member_query.refresh_cache(message.chat.id)
        await status_msg.edit_text(localizer.t("cleanup.refresh.success.message", count=count))
        return status_msg  # 返回消息对象以便中间件自动删除
    except Exception as e:
        logger.error(f"刷新缓存失败: {e}")
        await status_msg.edit_text(
            localizer.t("cleanup.error.refresh_failed.message", error=escape_html(str(e)))
        )
        return status_msg  # 返回消息对象以便中间件自动删除


async def _handle_cache_info(
    message: Message, member_query: MemberQueryService, localizer: BoundLocalizer
) -> Message | None:
    """处理查看缓存信息"""
    if not message.chat:
        return None

    try:
        cache_info = await member_query.get_cache_info(message.chat.id)
        if not cache_info:
            return await message.answer(localizer.t("cleanup.cache.missing.message"))

        cached_at = datetime.fromisoformat(cache_info["cached_at"])
        ttl_minutes = cache_info["ttl_seconds"] // 60

        return await message.answer(
            localizer.t(
                "cleanup.cache.info.message",
                member_count=cache_info["member_count"],
                cached_at=cached_at.strftime("%Y-%m-%d %H:%M:%S"),
                ttl_minutes=ttl_minutes,
            )
        )
    except Exception as e:
        logger.error(f"获取缓存信息失败: {e}")
        return await message.answer(
            localizer.t("cleanup.error.cache_lookup_failed.message", error=escape_html(str(e)))
        )


async def _handle_preview(
    message: Message, member_query: MemberQueryService, localizer: BoundLocalizer
) -> Message | None:
    """处理预览清理"""
    if not message.chat:
        return None

    status_msg = await message.answer(localizer.t("cleanup.preview.scanning.message"))

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)

        restricted_count = len(result["restricted"])
        scam_count = len(result["scam"])
        fake_count = len(result["fake"])
        deleted_count = len(result["deleted"])
        total = restricted_count + scam_count + fake_count + deleted_count

        if total == 0:
            await status_msg.edit_text(localizer.t("cleanup.preview.empty.message"))
            return status_msg

        await status_msg.edit_text(
            localizer.t(
                "cleanup.preview.result.message",
                restricted_count=restricted_count,
                scam_count=scam_count,
                fake_count=fake_count,
                deleted_count=deleted_count,
                total=total,
            )
        )
        return status_msg
    except Exception as e:
        logger.error(f"预览清理失败: {e}")
        await status_msg.edit_text(
            localizer.t("cleanup.error.preview_failed.message", error=escape_html(str(e)))
        )
        return status_msg


async def _handle_run(
    message: Message,
    bot: Bot,
    member_query: MemberQueryService,
    localizer: BoundLocalizer,
) -> Message | None:
    """处理执行完整清理"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer(localizer.t("cleanup.preview.scanning.message"))

    try:
        # 获取待清理用户
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)

        restricted_users = result["restricted"]
        scam_users = result["scam"]
        fake_users = result["fake"]
        deleted_users = result["deleted"]
        total = len(restricted_users) + len(scam_users) + len(fake_users) + len(deleted_users)

        if total == 0:
            await status_msg.edit_text(localizer.t("cleanup.preview.empty.message"))
            return status_msg

        await status_msg.edit_text(
            localizer.t(
                "cleanup.run.start.message",
                total=total,
                restricted_count=len(restricted_users),
                scam_count=len(scam_users),
                fake_count=len(fake_users),
                deleted_count=len(deleted_users),
            )
        )

        # 执行清理
        cleanup_result = CleanupResult()

        # 清理受限用户
        if restricted_users:
            restricted_result = await CleanupService.execute_cleanup(
                bot,
                message.chat.id,
                restricted_users,
                message.from_user.id,
                CleanupReason.restricted,
            )
            cleanup_result.restricted_kicked = restricted_result.restricted_kicked
            cleanup_result.restricted_failed = restricted_result.restricted_failed
            cleanup_result.errors.extend(restricted_result.errors)

        # 清理诈骗用户
        if scam_users:
            scam_result = await CleanupService.execute_cleanup(
                bot,
                message.chat.id,
                scam_users,
                message.from_user.id,
                CleanupReason.scam,
            )
            cleanup_result.scam_kicked = scam_result.scam_kicked
            cleanup_result.scam_failed = scam_result.scam_failed
            cleanup_result.errors.extend(scam_result.errors)

        # 清理虚假用户
        if fake_users:
            fake_result = await CleanupService.execute_cleanup(
                bot,
                message.chat.id,
                fake_users,
                message.from_user.id,
                CleanupReason.fake,
            )
            cleanup_result.fake_kicked = fake_result.fake_kicked
            cleanup_result.fake_failed = fake_result.fake_failed
            cleanup_result.errors.extend(fake_result.errors)

        # 清理已删除用户
        if deleted_users:
            deleted_result = await CleanupService.execute_cleanup(
                bot,
                message.chat.id,
                deleted_users,
                message.from_user.id,
                CleanupReason.deleted,
            )
            cleanup_result.deleted_kicked = deleted_result.deleted_kicked
            cleanup_result.deleted_failed = deleted_result.deleted_failed
            cleanup_result.errors.extend(deleted_result.errors)

        # 显示结果
        await _show_cleanup_result(status_msg, cleanup_result, localizer)
        return status_msg

    except Exception as e:
        logger.error(f"执行清理失败: {e}")
        await status_msg.edit_text(
            localizer.t("cleanup.error.execution_failed.message", error=escape_html(str(e)))
        )
        return status_msg


async def _handle_deleted(
    message: Message,
    bot: Bot,
    member_query: MemberQueryService,
    localizer: BoundLocalizer,
) -> Message | None:
    """处理仅清理已删除用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer(localizer.t("cleanup.deleted.scanning.message"))

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        deleted_users = result["deleted"]

        if not deleted_users:
            await status_msg.edit_text(localizer.t("cleanup.deleted.empty.message"))
            return status_msg

        await status_msg.edit_text(
            localizer.t("cleanup.deleted.start.message", count=len(deleted_users))
        )

        cleanup_result = await CleanupService.execute_cleanup(
            bot,
            message.chat.id,
            deleted_users,
            message.from_user.id,
            CleanupReason.deleted,
        )

        await _show_cleanup_result(status_msg, cleanup_result, localizer)
        return status_msg

    except Exception as e:
        logger.error(f"清理已删除用户失败: {e}")
        await status_msg.edit_text(
            localizer.t("cleanup.error.execution_failed.message", error=escape_html(str(e)))
        )
        return status_msg


async def _handle_restricted(
    message: Message,
    bot: Bot,
    member_query: MemberQueryService,
    localizer: BoundLocalizer,
) -> Message | None:
    """处理仅清理受限用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer(localizer.t("cleanup.restricted.scanning.message"))

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        restricted_users = result["restricted"]

        if not restricted_users:
            await status_msg.edit_text(localizer.t("cleanup.restricted.empty.message"))
            return status_msg

        await status_msg.edit_text(
            localizer.t("cleanup.restricted.start.message", count=len(restricted_users))
        )

        cleanup_result = await CleanupService.execute_cleanup(
            bot,
            message.chat.id,
            restricted_users,
            message.from_user.id,
            CleanupReason.restricted,
        )

        await _show_cleanup_result(status_msg, cleanup_result, localizer)
        return status_msg

    except Exception as e:
        logger.error(f"清理受限用户失败: {e}")
        await status_msg.edit_text(
            localizer.t("cleanup.error.execution_failed.message", error=escape_html(str(e)))
        )
        return status_msg


async def _handle_scam(
    message: Message,
    bot: Bot,
    member_query: MemberQueryService,
    localizer: BoundLocalizer,
) -> Message | None:
    """处理仅清理诈骗标记用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer(localizer.t("cleanup.scam.scanning.message"))

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        scam_users = result["scam"]

        if not scam_users:
            await status_msg.edit_text(localizer.t("cleanup.scam.empty.message"))
            return status_msg

        await status_msg.edit_text(localizer.t("cleanup.scam.start.message", count=len(scam_users)))

        cleanup_result = await CleanupService.execute_cleanup(
            bot,
            message.chat.id,
            scam_users,
            message.from_user.id,
            CleanupReason.scam,
        )

        await _show_cleanup_result(status_msg, cleanup_result, localizer)
        return status_msg

    except Exception as e:
        logger.error(f"清理诈骗标记用户失败: {e}")
        await status_msg.edit_text(
            localizer.t("cleanup.error.execution_failed.message", error=escape_html(str(e)))
        )
        return status_msg


async def _handle_fake(
    message: Message,
    bot: Bot,
    member_query: MemberQueryService,
    localizer: BoundLocalizer,
) -> Message | None:
    """处理仅清理虚假标记用户"""
    if not message.chat or not message.from_user:
        return None

    status_msg = await message.answer(localizer.t("cleanup.fake.scanning.message"))

    try:
        result = await CleanupService.preview_cleanup(member_query, message.chat.id)
        fake_users = result["fake"]

        if not fake_users:
            await status_msg.edit_text(localizer.t("cleanup.fake.empty.message"))
            return status_msg

        await status_msg.edit_text(localizer.t("cleanup.fake.start.message", count=len(fake_users)))

        cleanup_result = await CleanupService.execute_cleanup(
            bot,
            message.chat.id,
            fake_users,
            message.from_user.id,
            CleanupReason.fake,
        )

        await _show_cleanup_result(status_msg, cleanup_result, localizer)
        return status_msg

    except Exception as e:
        logger.error(f"清理虚假标记用户失败: {e}")
        await status_msg.edit_text(
            localizer.t("cleanup.error.execution_failed.message", error=escape_html(str(e)))
        )
        return status_msg


def _render_cleanup_error(localizer: BoundLocalizer, error: CleanupError) -> str:
    """把服务层 error code 渲染为当前群组语言的安全文案。

    user_id 为整数无注入风险；detail 经 escape_html 后注入（None → 空串，
    对无 {detail} 占位符的 code 无影响——Translator 容忍多余变量）。
    """
    return localizer.t(
        f"cleanup.error.{error.code.value}.message",
        user_id=error.user_id,
        detail=escape_html(error.detail),
    )


async def _show_cleanup_result(
    message: Message, result: CleanupResult, localizer: BoundLocalizer
) -> None:
    """显示清理结果"""
    total_kicked = (
        result.restricted_kicked + result.scam_kicked + result.fake_kicked + result.deleted_kicked
    )
    total_failed = (
        result.restricted_failed + result.scam_failed + result.fake_failed + result.deleted_failed
    )

    text = localizer.t(
        "cleanup.result.summary.message",
        restricted_kicked=result.restricted_kicked,
        restricted_failed=result.restricted_failed,
        scam_kicked=result.scam_kicked,
        scam_failed=result.scam_failed,
        fake_kicked=result.fake_kicked,
        fake_failed=result.fake_failed,
        deleted_kicked=result.deleted_kicked,
        deleted_failed=result.deleted_failed,
        total_kicked=total_kicked,
        total_failed=total_failed,
    )

    if result.errors:
        text += "\n\n" + localizer.t(
            "cleanup.result.errors_header.message", error_count=len(result.errors)
        )
        for error in result.errors[:5]:
            text += "\n" + localizer.t(
                "cleanup.result.error_item.message",
                error=_render_cleanup_error(localizer, error),
            )

    await message.edit_text(text)
