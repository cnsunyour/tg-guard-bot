"""用户设置模型"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class UserSettings(Base):
    """用户私聊设置表

    采用稀疏写入语义：只有用户主动修改设置后才创建记录。
    没有记录时由 LocaleResolver 应用默认语言或来源群语言，
    因此“无记录”与“显式选择默认语言”需在解析层显式区分。
    """

    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        comment="Telegram 用户 ID",
    )
    locale: Mapped[str] = mapped_column(
        String(16),
        default="zh-CN",
        nullable=False,
        comment="用户私聊语言偏好（BCP 47，如 zh-CN/zh-TW/zh-HK/en）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        comment="最后更新时间",
    )

    def __repr__(self) -> str:
        return f"<UserSettings(user_id={self.user_id}, locale={self.locale})>"
