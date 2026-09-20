"""groups 增加 anti_external_reply_enabled 跨聊天回复防护开关

Revision ID: e8d8f4f6ed3e
Revises: 428bc0004879
Create Date: 2026-09-18 20:39:02.287252
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e8d8f4f6ed3e'
down_revision: str | Sequence[str] | None = '428bc0004879'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级数据库结构。

    autogenerate 输出已手动清理：去除 dev 库遗留的 ``schema_migrations`` 旧表
    drop、各列 server_default/comment 波动检测与 ``idx_groups_id`` 冗余索引
    （PG 主键自带索引），仅保留本次变更的加列。
    """
    op.add_column(
        'groups',
        sa.Column(
            'anti_external_reply_enabled',
            sa.Boolean(),
            server_default=sa.text('true'),
            nullable=False,
            comment='是否拦截跨聊天回复消息（Reply in Another Chat 引用外部消息预览的引流手法）',
        ),
    )


def downgrade() -> None:
    """回滚数据库结构（删除跨聊天回复防护开关列，可逆）。"""
    op.drop_column('groups', 'anti_external_reply_enabled')
