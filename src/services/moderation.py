"""群管理服务模块"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from aiogram import Bot
from aiogram.types import ChatPermissions
from loguru import logger

from src.core.config import settings
from src.repositories.audit_repo import AuditRepository
from src.repositories.user_repo import UserRepository


class ModerationErrorCode(StrEnum):
    """群管理操作失败原因的稳定 code。

    服务层只返回 code，不含任何用户可见文案；由 handler 层按当前群组
    locale 渲染对应 catalog 文案（``moderation.error.<code>.message``）。
    """

    user_not_in_chat = "user_not_in_chat"
    verify_user_failed = "verify_user_failed"
    target_is_admin = "target_is_admin"
    verify_admin_failed = "verify_admin_failed"
    operation_failed = "operation_failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ModerationResult:
    """群管理操作结果。

    ``code is None`` 表示成功；失败时始终携带一个稳定的错误 code。
    success 由 code 推导，避免 ``tuple[bool, str]`` 中 bool 与文案互相
    矛盾的隐患（例如 ``True, "失败原因"`` 这种非法组合）。
    """

    code: ModerationErrorCode | None = None

    @property
    def success(self) -> bool:
        return self.code is None


class ModerationService:
    """群管理服务"""

    @staticmethod
    async def verify_user_in_chat(bot: Bot, chat_id: int, user_id: int) -> ModerationResult:
        """✅ M7: 验证用户是否存在于群组中

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            ``ModerationResult``，成功表示用户在群组中。
        """
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            # 如果用户已经离开或被踢出
            if member.status in ["left", "kicked"]:
                return ModerationResult(code=ModerationErrorCode.user_not_in_chat)
            return ModerationResult()
        except Exception as e:
            logger.debug(f"验证用户存在性失败: {e}")
            return ModerationResult(code=ModerationErrorCode.verify_user_failed)

    @staticmethod
    async def verify_not_admin(bot: Bot, chat_id: int, user_id: int) -> ModerationResult:
        """验证目标用户不是管理员

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            ``ModerationResult``，成功表示目标用户不是管理员（可以操作）。
        """
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            # 检查是否是群主或管理员
            if member.status in ["creator", "administrator"]:
                return ModerationResult(code=ModerationErrorCode.target_is_admin)
            return ModerationResult()
        except Exception as e:
            logger.debug(f"验证管理员身份失败: {e}")
            return ModerationResult(code=ModerationErrorCode.verify_admin_failed)

    @staticmethod
    async def kick_user(
        bot: Bot,
        chat_id: int,
        user_id: int,
        operator_id: int,
        reason: str | None = None,
        revoke_messages: bool = False,
    ) -> ModerationResult:
        """踢出用户

        Args:
            revoke_messages: 是否删除该用户的所有消息（默认 False）

        Returns:
            ``ModerationResult``，成功表示已踢出。
        """
        try:
            # ✅ M7: 先验证用户是否在群组中
            verification = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
            if not verification.success:
                return verification

            # 验证目标用户不是管理员
            admin_check = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not admin_check.success:
                return admin_check

            # 踢出用户（临时封禁后立即解封）
            await bot.ban_chat_member(
                chat_id=chat_id, user_id=user_id, revoke_messages=revoke_messages
            )
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)

            # 记录日志
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="kick",
                target_user_id=user_id,
                details={"reason": reason} if reason else {},
            )

            logger.info(f"用户 {user_id} 被管理员 {operator_id} 踢出群组 {chat_id}")
            return ModerationResult()

        except Exception as e:
            logger.error(f"踢出用户失败: {e}")
            return ModerationResult(code=ModerationErrorCode.operation_failed)

    @staticmethod
    async def mute_user(
        bot: Bot,
        chat_id: int,
        user_id: int,
        operator_id: int,
        duration: int | None = None,
        reason: str | None = None,
    ) -> ModerationResult:
        """禁言用户

        Args:
            duration: 禁言时长（分钟），None 表示永久禁言

        Returns:
            ``ModerationResult``，成功表示已禁言。
        """
        try:
            # 防御：parse_duration 已校验，此处兜底拒绝非正时长（防其他入口绕过）
            if duration is not None and duration <= 0:
                logger.warning(f"拒绝无效禁言时长: {duration}")
                return ModerationResult(code=ModerationErrorCode.operation_failed)

            # ✅ M7: 先验证用户是否在群组中
            verification = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
            if not verification.success:
                return verification

            # 验证目标用户不是管理员
            admin_check = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not admin_check.success:
                return admin_check

            # 计算禁言到期时间
            until_date = None
            if duration is not None:
                until_date = datetime.utcnow() + timedelta(minutes=duration)

            # 限制用户权限（禁用所有权限）
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_audios=False,
                    can_send_documents=False,
                    can_send_photos=False,
                    can_send_videos=False,
                    can_send_video_notes=False,
                    can_send_voice_notes=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_react_to_messages=False,
                    can_edit_tag=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                    can_manage_topics=False,
                ),
                until_date=until_date,
            )

            # 记录日志
            details = {
                "duration": duration,
                "reason": reason,
                "until": until_date.isoformat() if until_date else None,
            }

            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="mute",
                target_user_id=user_id,
                details=details,
            )

            logger.info(
                f"用户 {user_id} 被管理员 {operator_id} 禁言 "
                f"{'永久' if duration is None else f'{duration}分钟'}"
            )
            return ModerationResult()

        except Exception as e:
            logger.error(f"禁言用户失败: {e}")
            return ModerationResult(code=ModerationErrorCode.operation_failed)

    @staticmethod
    async def _unban_or_unmute_user(
        bot: Bot, chat_id: int, user_id: int, operator_id: int, action: str
    ) -> bool:
        """解除封禁或禁言（统一实现）

        Args:
            action: 'unmute' 或 'unban'，仅用于日志记录

        修复方案：
        - kicked（封禁）：解除封禁
        - restricted（禁言）：恢复权限 + 30秒后自动从 restricted 列表移除
        """
        try:
            # 先获取用户当前状态
            member = await bot.get_chat_member(chat_id, user_id)

            # 根据状态采取不同操作
            if member.status == "kicked":
                # 用户被封禁，解除封禁
                await bot.unban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    only_if_banned=True,
                )

            elif member.status == "restricted":
                # 用户被禁言，恢复权限 + 31秒后自动移出 restricted 列表
                from datetime import datetime, timedelta

                from aiogram.types import ChatPermissions

                until_date = datetime.utcnow() + timedelta(seconds=31)

                # 只需设置 can_send_messages=True 即可恢复权限。
                # 在默认模式下（use_independent_chat_permissions=False），
                # can_send_messages=True 会通过群组默认权限隐式继承其他媒体权限
                # （can_send_photos, can_send_videos 等），用户在 31 秒后会从
                # restricted 列表移除，成为普通成员，拥有群组默认的所有权限。
                await bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                    ),
                    until_date=until_date,  # 30秒后自动从 restricted 列表移除
                )

            # 其他状态（member, left, administrator 等）无需操作

            # 记录日志
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action=action,
                target_user_id=user_id,
            )

            action_text = "解除禁言" if action == "unmute" else "解除封禁"
            logger.info(
                f"用户 {user_id} 被管理员 {operator_id} {action_text} (原状态: {member.status})"
            )
            return True

        except Exception as e:
            action_text = "解除禁言" if action == "unmute" else "解除封禁"
            logger.error(f"{action_text}失败: {e}")
            return False

    @staticmethod
    async def unmute_user(bot: Bot, chat_id: int, user_id: int, operator_id: int) -> bool:
        """解除禁言

        注：实际上会解除用户的所有限制（包括封禁和禁言），与 unban_user 完全相同
        """
        return await ModerationService._unban_or_unmute_user(
            bot, chat_id, user_id, operator_id, "unmute"
        )

    @staticmethod
    async def unban_user(bot: Bot, chat_id: int, user_id: int, operator_id: int) -> bool:
        """解除封禁

        注：实际上会解除用户的所有限制（包括封禁和禁言），与 unmute_user 完全相同
        """
        return await ModerationService._unban_or_unmute_user(
            bot, chat_id, user_id, operator_id, "unban"
        )

    @staticmethod
    async def ban_user(
        bot: Bot,
        chat_id: int,
        user_id: int,
        operator_id: int,
        reason: str | None = None,
        revoke_messages: bool = False,
        *,
        allow_left: bool = False,
    ) -> ModerationResult:
        """永久封禁用户

        Args:
            revoke_messages: 是否删除该用户的所有消息
            allow_left: 是否允许封禁已离群/已被踢出的用户。人工复核 ban 需开启，
                防止 spammer 在管理员确认前退群规避处罚；Telegram ``ban_chat_member``
                对 left/kicked 用户也能执行（永久封禁 = 加入黑名单）。默认 False
                保持旧行为：普通封禁仍要求目标用户当前在群内。

        Returns:
            ``ModerationResult``，成功表示已封禁。
        """
        try:
            if not allow_left:
                # 默认行为不变：普通封禁仍要求目标用户当前在群内
                verification = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
                if not verification.success:
                    return verification

            # 即使 allow_left 也不可封禁管理员
            admin_check = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not admin_check.success:
                return admin_check

            # 封禁用户
            await bot.ban_chat_member(
                chat_id=chat_id, user_id=user_id, revoke_messages=revoke_messages
            )

            # 记录日志
            details: dict[str, str | bool] = {"reason": reason} if reason else {}
            if revoke_messages:
                details["revoke_messages"] = True

            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="ban",
                target_user_id=user_id,
                details=details,
            )

            logger.info(
                f"用户 {user_id} 被管理员 {operator_id} 永久封禁"
                + (" (已删除所有消息)" if revoke_messages else "")
            )
            return ModerationResult()

        except Exception as e:
            logger.error(f"封禁用户失败: {e}")
            return ModerationResult(code=ModerationErrorCode.operation_failed)

    @staticmethod
    async def ban_user_temporarily(
        bot: Bot,
        chat_id: int,
        user_id: int,
        operator_id: int,
        duration: int,
        reason: str | None = None,
    ) -> ModerationResult:
        """踢出用户并临时封禁

        Args:
            duration: 封禁时长（分钟）

        Returns:
            ``ModerationResult``，成功表示已临时封禁。
        """
        try:
            # ✅ M7: 先验证用户是否在群组中
            verification = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
            if not verification.success:
                return verification

            # 验证目标用户不是管理员
            admin_check = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not admin_check.success:
                return admin_check

            # 计算封禁到期时间
            until_date = datetime.utcnow() + timedelta(minutes=duration)

            # 踢出用户并临时封禁
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until_date)

            # 记录日志
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="ban_temp",
                target_user_id=user_id,
                details=(
                    {"reason": reason, "duration_minutes": duration}
                    if reason
                    else {"duration_minutes": duration}
                ),
            )

            logger.info(f"用户 {user_id} 被管理员 {operator_id} 踢出并封禁 {duration} 分钟")
            return ModerationResult()

        except Exception as e:
            logger.error(f"临时封禁用户失败: {e}")
            return ModerationResult(code=ModerationErrorCode.operation_failed)

    @staticmethod
    async def warn_user(
        bot: Bot,
        chat_id: int,
        user_id: int,
        operator_id: int,
        reason: str | None = None,
    ) -> tuple[bool, int, bool]:
        """警告用户

        Returns:
            (是否成功, 累计警告次数, 是否触发自动处罚)
        """
        try:
            # 验证目标用户不是管理员
            admin_check = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not admin_check.success:
                assert admin_check.code is not None
                logger.warning(
                    f"尝试警告管理员 {user_id}，操作已阻止 [code:{admin_check.code.value}]"
                )
                return False, 0, False

            # 添加警告
            await UserRepository.add_warning(
                group_id=chat_id, user_id=user_id, reason=reason, issued_by=operator_id
            )

            # 统计最近N天内的警告次数（使用配置的有效期）
            warning_count = await UserRepository.count_recent_warnings(
                chat_id, user_id, days=settings.warning_expiration_days
            )

            # 记录日志
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="warn",
                target_user_id=user_id,
                details={"reason": reason, "total_warnings": warning_count},
            )

            logger.info(
                f"用户 {user_id} 被管理员 {operator_id} 警告，累计警告次数: {warning_count}"
            )

            # 检查是否触发自动处罚（处罚升级机制）
            auto_punished = False

            # 阶段3: 封禁（踢出+拉黑）
            if warning_count >= settings.warning_ban_threshold:
                punishment = await ModerationService.ban_user(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    operator_id=operator_id,
                    reason=f"累计警告达到 {warning_count} 次",
                )
                auto_punished = punishment.success

                if punishment.success:
                    logger.info(f"用户 {user_id} 因累计 {warning_count} 次警告被自动封禁")
                else:
                    assert punishment.code is not None
                    logger.error(f"自动封禁失败: {punishment.code.value}")

            # 阶段2: 踢出群组
            elif warning_count >= settings.warning_kick_threshold:
                punishment = await ModerationService.kick_user(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    operator_id=operator_id,
                    reason=f"累计警告达到 {warning_count} 次",
                )
                auto_punished = punishment.success

                if punishment.success:
                    logger.info(f"用户 {user_id} 因累计 {warning_count} 次警告被自动踢出")
                else:
                    assert punishment.code is not None
                    logger.error(f"自动踢出失败: {punishment.code.value}")

            # 阶段1: 禁言
            elif warning_count >= settings.max_warnings:
                punishment = await ModerationService.mute_user(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    operator_id=operator_id,
                    duration=settings.warning_mute_duration_hours * 60,  # 转换为分钟
                    reason=f"累计警告达到 {warning_count} 次",
                )
                auto_punished = punishment.success

                if punishment.success:
                    logger.info(
                        f"用户 {user_id} 因累计 {warning_count} 次警告被自动禁言 "
                        f"{settings.warning_mute_duration_hours} 小时"
                    )
                else:
                    assert punishment.code is not None
                    logger.error(f"自动禁言失败: {punishment.code.value}")

            return True, warning_count, auto_punished

        except Exception as e:
            logger.error(f"警告用户失败: {e}")
            return False, 0, False

    @staticmethod
    async def clear_warnings(chat_id: int, user_id: int, operator_id: int) -> tuple[bool, int]:
        """清除用户警告

        Returns:
            (是否成功, 清除的警告数量)
        """
        try:
            # 清除警告
            count = await UserRepository.clear_warnings(chat_id, user_id)

            # 记录日志
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="clear_warnings",
                target_user_id=user_id,
                details={"cleared_count": count},
            )

            logger.info(f"用户 {user_id} 的 {count} 条警告被管理员 {operator_id} 清除")
            return True, count

        except Exception as e:
            logger.error(f"清除警告失败: {e}")
            return False, 0

    @staticmethod
    async def delete_messages_before(
        bot: Bot, chat_id: int, start_message_id: int, count: int, operator_id: int
    ) -> tuple[int, int]:
        """删除指定消息往前（更早）的N条消息（包含起始消息）

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            start_message_id: 起始消息 ID（包含）
            count: 要删除的消息总数（包含起始消息）
            operator_id: 操作者 ID

        Returns:
            (成功删除数量, 失败数量)
        """
        success_count = 0
        fail_count = 0

        # 从起始消息往前删除（消息ID递减）
        # count 是总数，包含起始消息，所以删除 start_message_id 到 start_message_id-(count-1)
        for i in range(count):
            message_id = start_message_id - i
            if message_id <= 0:
                break

            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                success_count += 1
            except Exception as e:
                logger.debug(f"删除消息 {message_id} 失败: {e}")
                fail_count += 1

        # 记录日志
        await AuditRepository.log_action(
            group_id=chat_id,
            operator_id=operator_id,
            action="delete_messages_before",
            details={
                "start_message_id": start_message_id,
                "count": count,
                "success": success_count,
                "failed": fail_count,
            },
        )

        logger.info(
            f"管理员 {operator_id} 删除了消息 {start_message_id} 往前共 {count} 条消息"
            f"（成功: {success_count}, 失败: {fail_count}）"
        )
        return success_count, fail_count

    @staticmethod
    async def delete_messages_after(
        bot: Bot, chat_id: int, start_message_id: int, count: int, operator_id: int
    ) -> tuple[int, int]:
        """删除指定消息往后（更晚）的N条消息（包含起始消息）

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            start_message_id: 起始消息 ID（包含）
            count: 要删除的消息总数（包含起始消息）
            operator_id: 操作者 ID

        Returns:
            (成功删除数量, 失败数量)
        """
        success_count = 0
        fail_count = 0

        # 从起始消息往后删除（消息ID递增）
        # count 是总数，包含起始消息，所以删除 start_message_id 到 start_message_id+(count-1)
        for i in range(count):
            message_id = start_message_id + i

            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                success_count += 1
            except Exception as e:
                logger.debug(f"删除消息 {message_id} 失败: {e}")
                fail_count += 1

        # 记录日志
        await AuditRepository.log_action(
            group_id=chat_id,
            operator_id=operator_id,
            action="delete_messages_after",
            details={
                "start_message_id": start_message_id,
                "count": count,
                "success": success_count,
                "failed": fail_count,
            },
        )

        logger.info(
            f"管理员 {operator_id} 删除了消息 {start_message_id} 往后共 {count} 条消息"
            f"（成功: {success_count}, 失败: {fail_count}）"
        )
        return success_count, fail_count

    @staticmethod
    async def delete_messages_range(
        bot: Bot, chat_id: int, start_message_id: int, end_message_id: int, operator_id: int
    ) -> tuple[int, int]:
        """删除两个消息ID之间的所有消息（包含起止消息）

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            start_message_id: 起始消息 ID
            end_message_id: 结束消息 ID
            operator_id: 操作者 ID

        Returns:
            (成功删除数量, 失败数量)
        """
        # 确保 start <= end
        if start_message_id > end_message_id:
            start_message_id, end_message_id = end_message_id, start_message_id

        success_count = 0
        fail_count = 0

        # 遍历消息ID范围并删除
        for message_id in range(start_message_id, end_message_id + 1):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                success_count += 1
            except Exception as e:
                logger.debug(f"删除消息 {message_id} 失败: {e}")
                fail_count += 1

        # 记录日志
        await AuditRepository.log_action(
            group_id=chat_id,
            operator_id=operator_id,
            action="delete_messages_range",
            details={
                "start_message_id": start_message_id,
                "end_message_id": end_message_id,
                "success": success_count,
                "failed": fail_count,
            },
        )

        logger.info(
            f"管理员 {operator_id} 删除了消息范围 {start_message_id}-{end_message_id}"
            f"（成功: {success_count}, 失败: {fail_count}）"
        )
        return success_count, fail_count
