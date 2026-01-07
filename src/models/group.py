"""群组模型"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class Group(Base):
    """群组配置表"""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="Telegram chat_id")
    title: Mapped[str] = mapped_column(String(255), nullable=True, comment="群组标题")

    # 验证配置
    verification_type: Mapped[str] = mapped_column(
        String(20), default="math", comment="验证方式: math/slider/qa/emoji/captcha/honeypot/random"
    )
    verification_timeout: Mapped[int] = mapped_column(
        Integer, default=120, comment="验证超时时间(秒)"
    )

    # 反垃圾配置
    antispam_enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用反垃圾")
    antispam_level: Mapped[int] = mapped_column(
        Integer, default=2, comment="反垃圾严格程度: 1-宽松, 2-中等, 3-严格"
    )

    # 活跃度系统配置
    activity_enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用活跃度系统")

    # 白名单配置
    is_whitelisted: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否在白名单中")

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<Group(id={self.id}, title={self.title})>"
