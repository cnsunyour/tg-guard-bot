"""审计日志数据仓库"""

from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog
from src.core.database import get_db_session


class AuditRepository:
    """审计日志数据仓库"""

    @staticmethod
    async def log_action(
        group_id: int,
        operator_id: int,
        action: str,
        target_user_id: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """记录操作日志"""
        async with get_db_session() as session:
            log = AuditLog(
                group_id=group_id,
                operator_id=operator_id,
                action=action,
                target_user_id=target_user_id,
                details=details or {},
            )
            session.add(log)
            await session.commit()
            await session.refresh(log)
            return log

    @staticmethod
    async def get_logs(
        group_id: Optional[int] = None,
        operator_id: Optional[int] = None,
        action: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditLog]:
        """获取操作日志"""
        async with get_db_session() as session:
            query = select(AuditLog)

            filters = []
            if group_id is not None:
                filters.append(AuditLog.group_id == group_id)
            if operator_id is not None:
                filters.append(AuditLog.operator_id == operator_id)
            if action is not None:
                filters.append(AuditLog.action == action)

            if filters:
                query = query.where(and_(*filters))

            query = query.order_by(AuditLog.created_at.desc()).limit(limit)

            result = await session.execute(query)
            return list(result.scalars().all())

    @staticmethod
    async def get_recent_logs(
        group_id: int, hours: int = 24, limit: int = 50
    ) -> List[AuditLog]:
        """获取最近的操作日志"""
        async with get_db_session() as session:
            since = datetime.utcnow() - timedelta(hours=hours)
            result = await session.execute(
                select(AuditLog)
                .where(
                    and_(AuditLog.group_id == group_id, AuditLog.created_at >= since)
                )
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_user_actions(
        group_id: int, target_user_id: int, limit: int = 20
    ) -> List[AuditLog]:
        """获取针对特定用户的操作记录"""
        async with get_db_session() as session:
            result = await session.execute(
                select(AuditLog)
                .where(
                    and_(
                        AuditLog.group_id == group_id,
                        AuditLog.target_user_id == target_user_id,
                    )
                )
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())
