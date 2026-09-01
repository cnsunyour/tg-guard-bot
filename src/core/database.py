"""数据库连接管理模块"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger
from sqlalchemy import Select, delete, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import settings

# 分批删除的默认单批行数：每批独立提交，避免长事务与复制延迟/WAL 峰值。
# （moderation 的 100 条/批是 Telegram deleteMessages API 硬限制，与本常量无关）
DEFAULT_DELETE_BATCH_SIZE = 5000


# 声明式基类（SQLAlchemy 2.0 风格）
class Base(DeclarativeBase):
    """ORM 模型基类"""

    pass


# 全局引擎和会话工厂
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """获取数据库引擎"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取会话工厂"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话上下文管理器"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def delete_in_batches(
    session: AsyncSession,
    victim_ids_select: Select[tuple[Any]],
    target: type[Any],
    *,
    batch_size: int = DEFAULT_DELETE_BATCH_SIZE,
    extra_conditions: Sequence[Any] = (),
) -> int:
    """按 victim_ids_select 选取的主键分批删除 target 行

    target 须是带 ``id`` 主键列的 ORM 模型类。每批独立提交，避免长事务。
    victim_ids_select 须返回待删行主键（不含 limit，由本函数按 batch_size
    追加），并自带排序保证删除顺序。

    extra_conditions 会并入 DELETE 的 WHERE——用于并发防护，例如删除时
    再次校验选取时的过滤条件（行在选取与删除之间被并发修改则跳过）。

    Returns:
        删除的记录总数
    """
    total = 0
    while True:
        victim_result = await session.execute(victim_ids_select.limit(batch_size))
        victim_ids = [row[0] for row in victim_result.all()]
        if not victim_ids:
            break

        result = await session.execute(
            delete(target).where(target.id.in_(victim_ids), *extra_conditions)
        )
        await session.commit()
        # mypy: Result[Any] 实际上是 CursorResult，它有 rowcount 属性
        total += int(result.rowcount or 0)  # type: ignore[attr-defined]

        if len(victim_ids) < batch_size:
            break
    return total


async def init_db() -> None:
    """验证数据库连接。

    表结构由 Alembic 管理（容器启动时 ENTRYPOINT 执行 ``alembic upgrade head``），
    此处仅做连接健康检查，确保数据库可达后继续启动。
    """
    engine = get_engine()
    logger.info("验证数据库连接...")
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("✅ 数据库连接验证成功")


async def close_db() -> None:
    """关闭数据库连接"""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None


# ✅ P0-1: 提供 engine 作为模块级变量供向后兼容
# 这会在首次访问时懒加载引擎
def __getattr__(name: str):
    """模块级属性懒加载"""
    if name == "engine":
        return get_engine()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


# 显式导出供外部使用的对象
__all__ = [
    "DEFAULT_DELETE_BATCH_SIZE",
    "Base",
    "close_db",
    "delete_in_batches",
    "engine",  # 通过 __getattr__ 提供  # noqa: F822
    "get_db_session",
    "get_engine",
    "get_session_factory",
    "init_db",
]
