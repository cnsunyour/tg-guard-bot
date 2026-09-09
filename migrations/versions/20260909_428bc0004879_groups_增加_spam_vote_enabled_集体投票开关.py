"""groups 增加 spam_vote_enabled 集体投票开关

Revision ID: 428bc0004879
Revises: c3d35c9d5221
Create Date: 2026-09-09 04:39:05.347202
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '428bc0004879'
down_revision: str | Sequence[str] | None = 'c3d35c9d5221'
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
            'spam_vote_enabled',
            sa.Boolean(),
            server_default=sa.text('true'),
            nullable=False,
            comment='是否启用群成员集体投票（待确认垃圾/被举报消息由成员投票判定）',
        ),
    )


def downgrade() -> None:
    """回滚数据库结构（删除集体投票开关列，可逆）。"""
    op.drop_column('groups', 'spam_vote_enabled')
