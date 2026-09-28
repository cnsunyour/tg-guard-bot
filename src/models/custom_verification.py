"""自定义验证脚本 revision 模型（不可变版本化）"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.utils import utcnow_naive


class CustomVerificationRevision(Base):
    """群自定义验证脚本的一个不可变版本。

    设计：上传 = INSERT 新行（禁原地覆盖），Group.active_revision_id 指向
    当前生效版本。验证会话创建时绑定 revision，管理员上传/回滚只影响新
    会话；被引用的旧版本永久保留（无删除路径）。
    """

    __tablename__ = "custom_verification_revisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, index=True, nullable=False, comment="所属群组 chat_id"
    )
    language: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="脚本语言: python/javascript"
    )
    source: Mapped[str] = mapped_column(Text, nullable=False, comment="脚本源码全文")
    source_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="源码 SHA-256（会话绑定与变更检测）"
    )
    api_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), comment="脚本契约版本（当前固定 1）"
    )
    uploaded_by: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="上传管理员 user_id"
    )
    review_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="三重审查结论（静态违规/AI 结论/dry-run 结果），激活前必须齐全且通过",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, comment="创建时间")

    def __repr__(self) -> str:
        return f"<CustomVerificationRevision(id={self.id}, group_id={self.group_id}, language={self.language})>"
