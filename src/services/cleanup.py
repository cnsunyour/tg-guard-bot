"""群组用户清理服务"""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from loguru import logger

from src.services.member_query import MemberQueryService


class CleanupReason(StrEnum):
    """清理原因稳定 code（决定 _increment 计数字段 + handler 渲染标签）。"""

    restricted = "restricted"
    scam = "scam"
    fake = "fake"
    deleted = "deleted"


class CleanupErrorCode(StrEnum):
    """单个用户清理失败原因稳定 code（handler 据 code 选 catalog key 渲染）。"""

    target_is_admin = "target_is_admin"
    bot_permission_denied = "bot_permission_denied"
    telegram_bad_request = "telegram_bad_request"
    unexpected_error = "unexpected_error"


@dataclass(frozen=True, slots=True, kw_only=True)
class CleanupError:
    """服务层结构化清理错误：稳定 code + 渲染参数（不含中文文案）。

    detail 仅用于 telegram_bad_request / unexpected_error 的动态消息，
    handler 注入 catalog 前统一 escape_html；user_id 为整数无注入风险。
    """

    code: CleanupErrorCode
    user_id: int
    detail: str | None = None


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
    errors: list[CleanupError] = field(default_factory=list)


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
        reason: CleanupReason,
    ) -> CleanupResult:
        """执行批量踢出

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            user_ids: 用户 ID 列表
            _operator_id: 操作者 ID（未使用，保留接口）
            reason: 清理原因 code（决定 _increment 计数字段）

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
                        result.errors.append(
                            CleanupError(
                                code=CleanupErrorCode.target_is_admin,
                                user_id=user_id,
                            )
                        )
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
                result.errors.append(
                    CleanupError(
                        code=CleanupErrorCode.bot_permission_denied,
                        user_id=user_id,
                    )
                )
                CleanupService._increment_failed(result, reason)

            except TelegramBadRequest as e:
                error_msg = str(e)
                if "user not found" in error_msg.lower():
                    # 用户已不在群组，视为成功
                    logger.debug(f"用户 {user_id} 已不在群组")
                    continue

                result.errors.append(
                    CleanupError(
                        code=CleanupErrorCode.telegram_bad_request,
                        user_id=user_id,
                        detail=error_msg,
                    )
                )
                CleanupService._increment_failed(result, reason)

            except Exception as e:
                logger.error(f"踢出用户 {user_id} 失败: {e}")
                result.errors.append(
                    CleanupError(
                        code=CleanupErrorCode.unexpected_error,
                        user_id=user_id,
                        detail=str(e),
                    )
                )
                CleanupService._increment_failed(result, reason)

        return result

    @staticmethod
    def _increment_kicked(result: CleanupResult, reason: CleanupReason) -> None:
        """根据原因增加踢出计数"""
        if reason is CleanupReason.restricted:
            result.restricted_kicked += 1
        elif reason is CleanupReason.scam:
            result.scam_kicked += 1
        elif reason is CleanupReason.fake:
            result.fake_kicked += 1
        elif reason is CleanupReason.deleted:
            result.deleted_kicked += 1

    @staticmethod
    def _increment_failed(result: CleanupResult, reason: CleanupReason) -> None:
        """根据原因增加失败计数"""
        if reason is CleanupReason.restricted:
            result.restricted_failed += 1
        elif reason is CleanupReason.scam:
            result.scam_failed += 1
        elif reason is CleanupReason.fake:
            result.fake_failed += 1
        elif reason is CleanupReason.deleted:
            result.deleted_failed += 1
