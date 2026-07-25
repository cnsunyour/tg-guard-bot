"""群组数据仓库"""

from sqlalchemy import select

from src.core.database import get_db_session
from src.models.group import Group


class GroupRepository:
    """群组数据仓库"""

    @staticmethod
    async def get_or_create(chat_id: int, title: str | None = None) -> Group:
        """获取或创建群组配置"""
        async with get_db_session() as session:
            # 查询群组
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group is None:
                # 创建新群组
                group = Group(id=chat_id, title=title)
                session.add(group)
                await session.commit()
                await session.refresh(group)
            elif title is not None:
                # 群组已存在，更新 title（如果提供了新的 title）
                group.title = title
                await session.commit()
                await session.refresh(group)

            return group

    @staticmethod
    async def get(chat_id: int) -> Group | None:
        """获取群组配置"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(chat_id: int) -> Group | None:
        """根据 ID 获取群组配置（别名方法）"""
        return await GroupRepository.get(chat_id)

    @staticmethod
    async def update_verification_type(chat_id: int, verification_type: str) -> None:
        """更新验证方式"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.verification_type = verification_type
                await session.commit()

    @staticmethod
    async def update_verification_timeout(chat_id: int, timeout: int) -> None:
        """更新验证超时时间"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.verification_timeout = timeout
                await session.commit()

    @staticmethod
    async def update_antispam_settings(
        chat_id: int, enabled: bool, level: int | None = None
    ) -> None:
        """更新反垃圾设置"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.antispam_enabled = enabled
                if level is not None:
                    group.antispam_level = level
                await session.commit()

    @staticmethod
    async def update_whitelist(chat_id: int, is_whitelisted: bool) -> None:
        """更新群组白名单状态"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.is_whitelisted = is_whitelisted
                await session.commit()

    @staticmethod
    async def update_activity_settings(chat_id: int, enabled: bool) -> None:
        """更新活跃度系统设置"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.activity_enabled = enabled
                await session.commit()

    @staticmethod
    async def update_activity_skip_threshold(chat_id: int, threshold: int | None) -> None:
        """更新活跃度跳过垃圾检测阈值"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.activity_skip_threshold = threshold  # type: ignore[assignment]
                await session.commit()

    @staticmethod
    async def update_antichannel_settings(chat_id: int, enabled: bool) -> None:
        """更新反频道马甲设置"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.anti_channel_enabled = enabled
                await session.commit()

    @staticmethod
    async def update_spam_confirm_settings(chat_id: int, enabled: bool) -> bool:
        """更新垃圾消息管理员确认设置

        Args:
            chat_id: 群组 ID
            enabled: 是否启用管理员确认模式

        Returns:
            是否更新成功
        """
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.spam_confirm_enabled = enabled
                await session.commit()
                return True
            return False

    @staticmethod
    async def get_whitelisted_groups() -> list[Group]:
        """获取所有白名单群组"""
        async with get_db_session() as session:
            result = await session.execute(
                select(Group).where(Group.is_whitelisted).order_by(Group.id)
            )
            return list(result.scalars().all())

    @staticmethod
    async def update_curfew_settings(
        chat_id: int,
        enabled: bool,
        start_hour: int | None = None,
        start_minute: int | None = None,
        end_hour: int | None = None,
        end_minute: int | None = None,
        timezone_offset: int | None = None,
    ) -> bool:
        """更新宵禁设置

        Args:
            chat_id: 群组 ID
            enabled: 是否启用宵禁
            start_hour: 开始小时 (0-23)
            start_minute: 开始分钟 (0-59)
            end_hour: 结束小时 (0-23)
            end_minute: 结束分钟 (0-59)
            timezone_offset: 时区偏移（相对UTC小时数）

        Returns:
            是否更新成功
        """
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()

            if group:
                group.curfew_enabled = enabled
                if start_hour is not None:
                    group.curfew_start_hour = start_hour
                if start_minute is not None:
                    group.curfew_start_minute = start_minute
                if end_hour is not None:
                    group.curfew_end_hour = end_hour
                if end_minute is not None:
                    group.curfew_end_minute = end_minute
                if timezone_offset is not None:
                    group.curfew_timezone_offset = timezone_offset
                await session.commit()
                return True
            return False

    @staticmethod
    async def update_locale(chat_id: int, locale: str) -> bool:
        """更新群组消息语言

        Args:
            chat_id: Telegram 群组 ID
            locale: 已通过应用层校验的语言代码（BCP 47）

        Returns:
            是否找到并更新了群组
        """
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            group = result.scalar_one_or_none()
            if group is None:
                return False
            group.locale = locale
            await session.commit()
            return True
