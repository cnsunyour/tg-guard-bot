"""数据库连接管理模块"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import settings


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


async def init_db() -> None:
    """初始化数据库表"""
    engine = get_engine()
    async with engine.begin() as conn:
        # 导入所有模型以确保它们被注册
        from src.models import (  # noqa: F401
            audit_log,
            group,
            report,
            spam_sample,
            user,
            user_settings,
        )

        await conn.run_sync(Base.metadata.create_all)


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
    "Base",
    "close_db",
    "engine",  # 通过 __getattr__ 提供  # noqa: F822
    "get_db_session",
    "get_engine",
    "get_session_factory",
    "init_db",
]
