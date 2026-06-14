"""群组用户清理服务"""

import asyncio
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from loguru import logger

from src.services.member_query import MemberQueryService


@dataclass
class CleanupResult:
    """清理结果"""

    restricted_kicked: int = 0
    restricted_failed: int = 0
    scam_kicked: int = 0
    scam_failed: int = 0
    fake_kicked: int = 0
    fake_failed: int = 0
    deleted_kicked: int = 0
    deleted_failed: int = 0
    total_processed: int = 0
    errors: list[str] = field(default_factory=list)


class CleanupService:
    """群组用户清理服务"""

    # 每次踢出之间的延迟（秒），避免触发 Telegram 速率限制
    KICK_DELAY = 0.5

    @staticmethod
    async def preview_cleanup(
        member_query: MemberQueryService,
        chat_id: int,
    ) -> dict[str, list[int]]:
        """预览清理（不执行）

        Args:
            member_query: 成员查询服务
            chat_id: 群组 ID

        Returns:
            {
                "restricted": [user_ids],
                "scam": [user_ids],
                "fake": [user_ids],
                "deleted": [user_ids]
            }
        """
        return await member_query.get_problematic_users(chat_id)

    @staticmethod
    async def execute_cleanup(
        bot: Bot,
        chat_id: int,
        user_ids: list[int],
        _operator_id: int,
        reason: str,
    ) -> CleanupResult:
        """执行批量踢出

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            user_ids: 用户 ID 列表
            operator_id: 操作者 ID
            reason: 清理原因（restricted/scam/fake/deleted）

        Returns:
            CleanupResult 清理结果
        """
        result = CleanupResult()
        result.total_processed = len(user_ids)

        for user_id in user_ids:
            try:
                # 检查是否是管理员
                try:
                    member = await bot.get_chat_member(chat_id, user_id)
                    if member.status in ["creator", "administrator"]:
                        result.errors.append(f"{user_id}: 是管理员，跳过")
                        CleanupService._increment_failed(result, reason)
                        continue
                except TelegramBadRequest as e:
                    # 用户可能已不在群组
                    if "user not found" in str(e).lower():
                        logger.debug(f"用户 {user_id} 已不在群组")
                        continue
                    raise

                # 踢出用户（临时封禁后立即解封，允许重新加入）
                await bot.ban_chat_member(chat_id, user_id)
                await bot.unban_chat_member(chat_id, user_id)

                CleanupService._increment_kicked(result, reason)
                logger.info(f"已踢出用户 {user_id}: {reason}")

                # 延迟避免速率限制
                await asyncio.sleep(CleanupService.KICK_DELAY)

            except TelegramForbiddenError:
                result.errors.append(f"{user_id}: Bot 权限不足")
                CleanupService._increment_failed(result, reason)

            except TelegramBadRequest as e:
                error_msg = str(e)
                if "user not found" in error_msg.lower():
                    # 用户已不在群组，视为成功
                    logger.debug(f"用户 {user_id} 已不在群组")
                    continue

                result.errors.append(f"{user_id}: {error_msg}")
                CleanupService._increment_failed(result, reason)

            except Exception as e:
                logger.error(f"踢出用户 {user_id} 失败: {e}")
                result.errors.append(f"{user_id}: {e}")
                CleanupService._increment_failed(result, reason)

        return result

    @staticmethod
    def _increment_kicked(result: CleanupResult, reason: str) -> None:
        """根据原因增加踢出计数"""
        if "restricted" in reason.lower():
            result.restricted_kicked += 1
        elif "scam" in reason.lower():
            result.scam_kicked += 1
        elif "fake" in reason.lower():
            result.fake_kicked += 1
        elif "deleted" in reason.lower() or "已删除" in reason:
            result.deleted_kicked += 1

    @staticmethod
    def _increment_failed(result: CleanupResult, reason: str) -> None:
        """根据原因增加失败计数"""
        if "restricted" in reason.lower():
            result.restricted_failed += 1
        elif "scam" in reason.lower():
            result.scam_failed += 1
        elif "fake" in reason.lower():
            result.fake_failed += 1
        elif "deleted" in reason.lower() or "已删除" in reason:
            result.deleted_failed += 1
