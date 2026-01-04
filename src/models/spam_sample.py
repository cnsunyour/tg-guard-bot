"""垃圾样本模型"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class SpamSample(Base):
    """垃圾样本表（用于模型训练）"""

    __tablename__ = "spam_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, comment="消息内容")
    is_spam: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="是否为垃圾信息")
    confidence: Mapped[float] = mapped_column(Float, nullable=True, comment="置信度分数")
    labeled_by: Mapped[int] = mapped_column(BigInteger, nullable=True, comment="标注者 ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return f"<SpamSample(id={self.id}, is_spam={self.is_spam}, confidence={self.confidence})>"
