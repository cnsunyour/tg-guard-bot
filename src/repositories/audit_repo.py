"""审计日志数据仓库"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select

from src.core.database import DEFAULT_DELETE_BATCH_SIZE, delete_in_batches, get_db_session
from src.core.utils import utcnow_naive
from src.models.audit_log import AuditLog


class AuditRepository:
    """审计日志数据仓库"""

    @staticmethod
    async def log_action(
        group_id: int,
        operator_id: int,
        action: str,
        target_user_id: int | None = None,
        details: dict[str, Any] | None = None,
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
        group_id: int | None = None,
        operator_id: int | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[AuditLog]:
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
    async def get_recent_logs(group_id: int, hours: int = 24, limit: int = 50) -> list[AuditLog]:
        """获取最近的操作日志"""
        async with get_db_session() as session:
            since = utcnow_naive() - timedelta(hours=hours)
            result = await session.execute(
                select(AuditLog)
                .where(and_(AuditLog.group_id == group_id, AuditLog.created_at >= since))
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_user_actions(
        group_id: int, target_user_id: int, limit: int = 20
    ) -> list[AuditLog]:
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

    @staticmethod
    async def delete_logs_before(
        cutoff: datetime, batch_size: int = DEFAULT_DELETE_BATCH_SIZE
    ) -> int:
        """删除 cutoff 时间之前的审计日志（按时间从旧到新分批）

        Args:
            cutoff: 截止时间，created_at 早于此值的记录将被删除
            batch_size: 分批删除的单批数量，避免长事务

        Returns:
            删除的记录总数
        """
        async with get_db_session() as session:
            return await delete_in_batches(
                session,
                select(AuditLog.id)
                .where(AuditLog.created_at < cutoff)
                .order_by(AuditLog.created_at.asc(), AuditLog.id.asc()),
                AuditLog,
                batch_size=batch_size,
            )
