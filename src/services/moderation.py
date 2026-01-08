"""群管理服务模块"""

from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import ChatPermissions
from loguru import logger

from src.core.config import settings
from src.repositories.audit_repo import AuditRepository
from src.repositories.user_repo import UserRepository


class ModerationService:
    """群管理服务"""

    @staticmethod
    async def verify_user_in_chat(bot: Bot, chat_id: int, user_id: int) -> tuple[bool, str]:
        """✅ M7: 验证用户是否存在于群组中

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            (是否存在, 错误消息)
        """
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            # 如果用户已经离开或被踢出
            if member.status in ["left", "kicked"]:
                return False, "用户不在群组中"
            return True, ""
        except Exception as e:
            logger.debug(f"验证用户存在性失败: {e}")
            return False, "无法验证用户信息，请检查用户 ID 是否正确"

    @staticmethod
    async def verify_not_admin(bot: Bot, chat_id: int, user_id: int) -> tuple[bool, str]:
        """验证目标用户不是管理员

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            (是否通过检查, 错误消息) - True 表示不是管理员可以操作，False 表示是管理员不能操作
        """
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            # 检查是否是群主或管理员
            if member.status in ["creator", "administrator"]:
                return False, "无法对群组管理员执行此操作"
            return True, ""
        except Exception as e:
            logger.debug(f"验证管理员身份失败: {e}")
            return False, "无法验证用户权限信息"

    @staticmethod
    async def kick_user(
        bot: Bot, chat_id: int, user_id: int, operator_id: int, reason: str | None = None
    ) -> tuple[bool, str | None]:
        """踢出用户

        Returns:
            (是否成功, 错误消息)
        """
        try:
            # ✅ M7: 先验证用户是否在群组中
            exists, error_msg = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
            if not exists:
                return False, error_msg

            # 验证目标用户不是管理员
            not_admin, error_msg = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not not_admin:
                return False, error_msg

            # 踢出用户（临时封禁后立即解封）
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
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
            return True, None

        except Exception as e:
            logger.error(f"踢出用户失败: {e}")
            return False, "操作失败，请检查 Bot 权限"

    @staticmethod
    async def mute_user(
        bot: Bot,
        chat_id: int,
        user_id: int,
        operator_id: int,
        duration: int | None = None,
        reason: str | None = None,
    ) -> tuple[bool, str | None]:
        """禁言用户

        Args:
            duration: 禁言时长（分钟），None 表示永久禁言

        Returns:
            (是否成功, 错误消息)
        """
        try:
            # ✅ M7: 先验证用户是否在群组中
            exists, error_msg = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
            if not exists:
                return False, error_msg

            # 验证目标用户不是管理员
            not_admin, error_msg = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not not_admin:
                return False, error_msg

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
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                    can_manage_topics=False,
                ),
                until_date=until_date,
            )

            # 记录日志
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="mute",
                target_user_id=user_id,
                details={
                    "duration": duration,
                    "reason": reason,
                    "until": until_date.isoformat() if until_date else None,
                },
            )

            logger.info(
                f"用户 {user_id} 被管理员 {operator_id} 禁言 "
                f"{'永久' if duration is None else f'{duration}分钟'}"
            )
            return True, None

        except Exception as e:
            logger.error(f"禁言用户失败: {e}")
            return False, "操作失败，请检查 Bot 权限"

    @staticmethod
    async def _unban_or_unmute_user(
        bot: Bot, chat_id: int, user_id: int, operator_id: int, action: str
    ) -> bool:
        """解除封禁或禁言（统一实现）

        Args:
            action: 'unmute' 或 'unban'，仅用于日志记录

        无论用户是被封禁(ban)还是被限制(restrict)，统一解除所有限制
        """
        try:
            # 使用 unban_chat_member 将用户从任何限制状态中移除
            # only_if_banned=False 表示无论是 ban 还是 restrict 都解除
            await bot.unban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                only_if_banned=False,  # 关键：解除所有限制
            )

            # 记录日志
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action=action,
                target_user_id=user_id,
            )

            action_text = "解除禁言" if action == "unmute" else "解除封禁"
            logger.info(f"用户 {user_id} 被管理员 {operator_id} {action_text}并从限制列表中移除")
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
    ) -> tuple[bool, str | None]:
        """永久封禁用户

        Args:
            revoke_messages: 是否删除该用户的所有消息

        Returns:
            (是否成功, 错误消息)
        """
        try:
            # ✅ M7: 先验证用户是否在群组中
            exists, error_msg = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
            if not exists:
                return False, error_msg

            # 验证目标用户不是管理员
            not_admin, error_msg = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not not_admin:
                return False, error_msg

            # 封禁用户
            await bot.ban_chat_member(
                chat_id=chat_id, user_id=user_id, revoke_messages=revoke_messages
            )

            # 记录日志
            details = {"reason": reason} if reason else {}
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
            return True, None

        except Exception as e:
            logger.error(f"封禁用户失败: {e}")
            return False, "操作失败，请检查 Bot 权限"

    @staticmethod
    async def ban_user_temporarily(
        bot: Bot,
        chat_id: int,
        user_id: int,
        operator_id: int,
        duration: int,
        reason: str | None = None,
    ) -> tuple[bool, str | None]:
        """踢出用户并临时封禁

        Args:
            duration: 封禁时长（分钟）

        Returns:
            (是否成功, 错误消息)
        """
        try:
            # ✅ M7: 先验证用户是否在群组中
            exists, error_msg = await ModerationService.verify_user_in_chat(bot, chat_id, user_id)
            if not exists:
                return False, error_msg

            # 验证目标用户不是管理员
            not_admin, error_msg = await ModerationService.verify_not_admin(bot, chat_id, user_id)
            if not not_admin:
                return False, error_msg

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
            return True, None

        except Exception as e:
            logger.error(f"临时封禁用户失败: {e}")
            return False, "操作失败，请检查 Bot 权限"

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
                f"用户 {user_id} 被管理员 {operator_id} 警告，" f"累计警告次数: {warning_count}"
            )

            # 检查是否触发自动处罚（处罚升级机制）
            auto_punished = False

            # 阶段3: 封禁（踢出+拉黑）
            if warning_count >= settings.warning_ban_threshold:
                success, error_msg = await ModerationService.ban_user(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    operator_id=operator_id,
                    reason=f"累计警告达到 {warning_count} 次",
                )
                auto_punished = success

                if success:
                    logger.info(f"用户 {user_id} 因累计 {warning_count} 次警告被自动封禁")
                else:
                    logger.error(f"自动封禁失败: {error_msg}")

            # 阶段2: 踢出群组
            elif warning_count >= settings.warning_kick_threshold:
                success, error_msg = await ModerationService.kick_user(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    operator_id=operator_id,
                    reason=f"累计警告达到 {warning_count} 次",
                )
                auto_punished = success

                if success:
                    logger.info(f"用户 {user_id} 因累计 {warning_count} 次警告被自动踢出")
                else:
                    logger.error(f"自动踢出失败: {error_msg}")

            # 阶段1: 禁言
            elif warning_count >= settings.max_warnings:
                success, error_msg = await ModerationService.mute_user(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    operator_id=operator_id,
                    duration=settings.warning_mute_duration_hours * 60,  # 转换为分钟
                    reason=f"累计警告达到 {warning_count} 次",
                )
                auto_punished = success

                if success:
                    logger.info(
                        f"用户 {user_id} 因累计 {warning_count} 次警告被自动禁言 "
                        f"{settings.warning_mute_duration_hours} 小时"
                    )
                else:
                    logger.error(f"自动禁言失败: {error_msg}")

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
