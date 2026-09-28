"""custom_verification_revisions 自定义验证脚本版本表

Revision ID: 40ab00e0aff1
Revises: e8d8f4f6ed3e
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '40ab00e0aff1'
down_revision: str | Sequence[str] | None = 'e8d8f4f6ed3e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增脚本版本表 + groups 两列（纯增量，无既有结构改动）。"""
    op.create_table(
        'custom_verification_revisions',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('group_id', sa.BigInteger(), nullable=False, comment='所属群组 chat_id'),
        sa.Column('language', sa.String(length=20), nullable=False, comment='脚本语言: python/javascript'),
        sa.Column('source', sa.Text(), nullable=False, comment='脚本源码全文'),
        sa.Column('source_sha256', sa.String(length=64), nullable=False, comment='源码 SHA-256（会话绑定与变更检测）'),
        sa.Column('api_version', sa.Integer(), nullable=False, server_default='1', comment='脚本契约版本（当前固定 1）'),
        sa.Column('uploaded_by', sa.BigInteger(), nullable=False, comment='上传管理员 user_id'),
        sa.Column('review_result', JSONB(), nullable=True, comment='三重审查结论（静态违规/AI 结论/dry-run 结果）'),
        sa.Column('created_at', sa.DateTime(), nullable=False, comment='创建时间'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_custom_verification_revisions_group_id'),
        'custom_verification_revisions',
        ['group_id'],
    )
    op.add_column(
        'groups',
        sa.Column(
            'custom_verify_enabled',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
            comment='是否启用自定义验证脚本（启用时覆盖 verification_type，沙盒不可用时回落原类型）',
        ),
    )
    op.add_column(
        'groups',
        sa.Column(
            'active_revision_id',
            sa.BigInteger(),
            nullable=True,
            comment='当前生效的自定义脚本 revision id（NULL=未启用脚本验证）',
        ),
    )


def downgrade() -> None:
    op.drop_column('groups', 'active_revision_id')
    op.drop_column('groups', 'custom_verify_enabled')
    op.drop_index(
        op.f('ix_custom_verification_revisions_group_id'),
        table_name='custom_verification_revisions',
    )
    op.drop_table('custom_verification_revisions')
