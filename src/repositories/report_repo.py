"""举报记录数据仓库"""

from typing import List, Optional
from datetime import datetime
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.report import Report
from src.core.database import get_db_session


class ReportRepository:
    """举报记录数据仓库"""

    @staticmethod
    async def create_report(
        group_id: int,
        reporter_id: int,
        reported_user_id: int,
        message_id: int,
        message_text: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Report:
        """创建举报记录

        Args:
            group_id: 群组ID
            reporter_id: 举报者ID
            reported_user_id: 被举报者ID
            message_id: 消息ID
            message_text: 消息文本
            reason: 举报原因
        """
        async with get_db_session() as session:
            report = Report(
                group_id=group_id,
                reporter_id=reporter_id,
                reported_user_id=reported_user_id,
                message_id=message_id,
                message_text=message_text,
                reason=reason,
                status="pending",
            )
            session.add(report)
            await session.commit()
            await session.refresh(report)
            return report

    @staticmethod
    async def get_pending_reports(group_id: int, limit: int = 10) -> List[Report]:
        """获取待处理的举报

        Args:
            group_id: 群组ID
            limit: 限制数量
        """
        async with get_db_session() as session:
            query = (
                select(Report)
                .where(
                    and_(
                        Report.group_id == group_id,
                        Report.status == "pending",
                    )
                )
                .order_by(Report.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(query)
            return list(result.scalars().all())

    @staticmethod
    async def get_report_by_id(report_id: int) -> Optional[Report]:
        """根据ID获取举报记录

        Args:
            report_id: 举报ID
        """
        async with get_db_session() as session:
            query = select(Report).where(Report.id == report_id)
            result = await session.execute(query)
            return result.scalar_one_or_none()

    @staticmethod
    async def update_report_status(
        report_id: int,
        status: str,
        handled_by: int,
    ) -> bool:
        """更新举报状态

        Args:
            report_id: 举报ID
            status: 新状态 (approved/rejected)
            handled_by: 处理者ID
        """
        async with get_db_session() as session:
            query = select(Report).where(Report.id == report_id)
            result = await session.execute(query)
            report = result.scalar_one_or_none()

            if not report:
                return False

            report.status = status
            report.handled_by = handled_by
            report.handled_at = datetime.utcnow()

            await session.commit()
            return True

    @staticmethod
    async def count_pending_reports(group_id: int) -> int:
        """统计待处理举报数量

        Args:
            group_id: 群组ID
        """
        async with get_db_session() as session:
            query = select(func.count(Report.id)).where(
                and_(
                    Report.group_id == group_id,
                    Report.status == "pending",
                )
            )
            result = await session.execute(query)
            return result.scalar() or 0

    @staticmethod
    async def count_user_reports(
        group_id: int,
        reporter_id: int,
        days: int = 1,
    ) -> int:
        """统计用户在指定天数内的举报次数

        Args:
            group_id: 群组ID
            reporter_id: 举报者ID
            days: 天数
        """
        from datetime import timedelta

        async with get_db_session() as session:
            cutoff_time = datetime.utcnow() - timedelta(days=days)
            query = select(func.count(Report.id)).where(
                and_(
                    Report.group_id == group_id,
                    Report.reporter_id == reporter_id,
                    Report.created_at >= cutoff_time,
                )
            )
            result = await session.execute(query)
            return result.scalar() or 0
