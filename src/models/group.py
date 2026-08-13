"""群组模型"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.utils import utcnow_naive


class Group(Base):
    """群组配置表"""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="Telegram chat_id")
    title: Mapped[str] = mapped_column(String(255), nullable=True, comment="群组标题")

    # 验证配置
    verification_type: Mapped[str] = mapped_column(
        String(20),
        default="random",
        comment="验证方式: math/slider/qa/emoji/captcha/honeypot/random",
    )
    verification_timeout: Mapped[int] = mapped_column(
        Integer, default=120, comment="验证超时时间(秒)"
    )

    # 反垃圾配置
    antispam_enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用反垃圾")
    antispam_level: Mapped[int] = mapped_column(
        Integer, default=2, comment="反垃圾严格程度: 1-宽松, 2-中等, 3-严格"
    )
    spam_confirm_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        comment="是否启用管理员确认模式（检测到垃圾后等待管理员确认再处罚）",
    )

    # 反频道马甲配置
    anti_channel_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        comment="是否启用反频道马甲(禁止用户以频道身份发言)",
    )

    # 活跃度系统配置
    activity_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        comment="是否启用活跃度系统",
    )
    activity_skip_threshold: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        comment="活跃度跳过垃圾检测阈值（0=禁用，>0=启用并使用此阈值）",
    )

    # 宵禁模式配置
    curfew_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        comment="是否启用宵禁模式",
    )
    curfew_start_hour: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="宵禁开始小时 (0-23)"
    )
    curfew_start_minute: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        comment="宵禁开始分钟 (0-59)",
    )
    curfew_end_hour: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="宵禁结束小时 (0-23)"
    )
    curfew_end_minute: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        comment="宵禁结束分钟 (0-59)",
    )
    curfew_timezone_offset: Mapped[int] = mapped_column(
        Integer,
        default=8,
        server_default=text("8"),
        comment="时区偏移（相对UTC小时数）",
    )

    # i18n 多语言配置
    locale: Mapped[str] = mapped_column(
        String(16),
        default="zh-Hans",
        server_default=text("'zh-Hans'"),
        comment="群组消息语言（BCP 47，如 zh-Hans/zh-Hant/en）",
    )

    # 白名单配置
    is_whitelisted: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否在白名单中")

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        comment="更新时间",
    )

    def __repr__(self) -> str:
        return f"<Group(id={self.id}, title={self.title})>"
