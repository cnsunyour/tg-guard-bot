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
    async def get_whitelisted_groups() -> list[Group]:
        """获取所有白名单群组"""
        async with get_db_session() as session:
            result = await session.execute(
                select(Group).where(Group.is_whitelisted).order_by(Group.id)
            )
            return list(result.scalars().all())
