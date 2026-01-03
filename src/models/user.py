"""用户警告模型"""

from datetime import datetime
from sqlalchemy import BigInteger, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class Warning(Base):
    """用户警告表"""

    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), comment="群组 ID"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="被警告用户 ID")
    reason: Mapped[str] = mapped_column(Text, nullable=True, comment="警告原因")
    issued_by: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="警告发起者 ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return f"<Warning(id={self.id}, group_id={self.group_id}, user_id={self.user_id})>"
