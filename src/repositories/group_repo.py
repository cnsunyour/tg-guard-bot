"""群组数据仓库"""

from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.group import Group
from src.core.database import get_db_session


class GroupRepository:
    """群组数据仓库"""

    @staticmethod
    async def get_or_create(chat_id: int, title: Optional[str] = None) -> Group:
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

            return group

    @staticmethod
    async def get(chat_id: int) -> Optional[Group]:
        """获取群组配置"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.id == chat_id))
            return result.scalar_one_or_none()

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
        chat_id: int, enabled: bool, level: Optional[int] = None
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
