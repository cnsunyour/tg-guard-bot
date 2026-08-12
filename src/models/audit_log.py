"""操作日志模型"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.utils import utcnow_naive


class AuditLog(Base):
    """操作日志表"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="群组 ID")
    operator_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="操作者 ID")
    action: Mapped[str] = mapped_column(String(50), nullable=False, comment="操作类型")
    target_user_id: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="目标用户 ID")
    details: Mapped[dict] = mapped_column(JSONB, nullable=True, comment="操作详情")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, comment="创建时间")

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, action={self.action}, operator_id={self.operator_id})>"
