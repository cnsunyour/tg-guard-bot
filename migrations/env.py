"""Alembic 异步迁移环境。

连接串来自 ``src.core.config.settings.database_url``（postgresql+asyncpg），
不写入 alembic.ini 以避开 ConfigParser 的 ``%`` 插值问题（密码可能含 ``%``）。

- 在线模式（默认）：创建独立的 async engine（NullPool，不复用应用全局连接池），
  经 ``run_sync`` 在同步回调中执行迁移；迁移结束立即 dispose。
- 离线模式（``alembic upgrade head --sql``）：仅生成 SQL，不连接数据库，
  URL 去掉 ``+asyncpg`` 以使用纯 PostgreSQL 方言渲染。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings
from src.core.database import Base

# 显式导入全部模型模块，确保所有表注册到 Base.metadata。
# 注意：src/models/__init__.py 未导出 Report，这里必须直接 import 子模块，
# 否则 autogenerate 会漏掉 reports 表。
from src.models import (  # noqa: F401
    audit_log,
    group,
    report,
    spam_sample,
    user,
    user_settings,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure_context(**kwargs: object) -> None:
    """统一配置迁移上下文。

    - compare_type / compare_server_default：让 autogenerate 检测类型与默认值差异
    - render_as_batch=False：PostgreSQL 原生支持 ALTER，不需要 SQLite 兼容的 batch 模式
    """
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不连接数据库。

    去掉 ``+asyncpg`` 后缀，使用纯 PostgreSQL 方言渲染（离线无需 DBAPI 驱动）。
    """
    url = settings.database_url.replace("+asyncpg", "", 1)
    _configure_context(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations(connection: Connection) -> None:
    """同步回调：在已建立的连接上执行迁移操作。"""
    _configure_context(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """创建独立异步引擎执行迁移。

    使用 NullPool（迁移一次性运行，无需连接池），不复用应用的全局 engine，
    避免迁移 DDL 污染应用连接池状态。
    """
    engine = create_async_engine(
        settings.database_url,
        poolclass=pool.NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """在线模式入口：在新的事件循环中运行异步迁移。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
