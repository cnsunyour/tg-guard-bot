"""用户/警告数据仓库"""

from typing import List, Optional
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import Warning
from src.core.database import get_db_session


class UserRepository:
    """用户数据仓库"""

    @staticmethod
    async def add_warning(
        group_id: int, user_id: int, reason: Optional[str], issued_by: int
    ) -> Warning:
        """添加警告"""
        async with get_db_session() as session:
            warning = Warning(
                group_id=group_id,
                user_id=user_id,
                reason=reason,
                issued_by=issued_by,
            )
            session.add(warning)
            await session.commit()
            await session.refresh(warning)
            return warning

    @staticmethod
    async def get_warnings(group_id: int, user_id: int) -> List[Warning]:
        """获取用户在指定群组的所有警告"""
        async with get_db_session() as session:
            result = await session.execute(
                select(Warning)
                .where(and_(Warning.group_id == group_id, Warning.user_id == user_id))
                .order_by(Warning.created_at.desc())
            )
            return list(result.scalars().all())

    @staticmethod
    async def count_warnings(group_id: int, user_id: int) -> int:
        """统计用户在指定群组的警告次数"""
        async with get_db_session() as session:
            result = await session.execute(
                select(func.count(Warning.id)).where(
                    and_(Warning.group_id == group_id, Warning.user_id == user_id)
                )
            )
            return result.scalar() or 0

    @staticmethod
    async def count_recent_warnings(
        group_id: int, user_id: int, days: int = 30
    ) -> int:
        """统计用户最近N天内的警告次数"""
        async with get_db_session() as session:
            since = datetime.utcnow() - timedelta(days=days)
            result = await session.execute(
                select(func.count(Warning.id)).where(
                    and_(
                        Warning.group_id == group_id,
                        Warning.user_id == user_id,
                        Warning.created_at >= since,
                    )
                )
            )
            return result.scalar() or 0

    @staticmethod
    async def clear_warnings(group_id: int, user_id: int) -> int:
        """清除用户在指定群组的所有警告

        ✅ P0-5: 使用 DELETE 语句直接删除，避免跨会话操作
        """
        async with get_db_session() as session:
            # 使用 DELETE 语句直接删除
            from sqlalchemy import delete

            result = await session.execute(
                delete(Warning)
                .where(and_(Warning.group_id == group_id, Warning.user_id == user_id))
            )
            await session.commit()
            # 返回删除的行数
            return result.rowcount or 0

    @staticmethod
    async def get_latest_warning(
        group_id: int, user_id: int
    ) -> Optional[Warning]:
        """获取用户最新的一条警告"""
        async with get_db_session() as session:
            result = await session.execute(
                select(Warning)
                .where(and_(Warning.group_id == group_id, Warning.user_id == user_id))
                .order_by(Warning.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
