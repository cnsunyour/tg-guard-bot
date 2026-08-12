"""举报记录模型"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.utils import utcnow_naive


class Report(Base):
    """举报记录"""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reporter_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reported_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )  # pending, approved, rejected, ignored
    handled_by: Mapped[int] = mapped_column(BigInteger, nullable=True)
    handled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, nullable=False)

    def __repr__(self):
        return (
            f"<Report(id={self.id}, group_id={self.group_id}, "
            f"reported_user_id={self.reported_user_id}, status={self.status})>"
        )
