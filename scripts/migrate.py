"""数据库迁移脚本 - 初始化数据库表"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from sqlalchemy import text

from src.core.database import init_db, engine, close_db
from src.models.group import Group
from src.models.user import Warning
from src.models.spam_sample import SpamSample
from src.models.audit_log import AuditLog


async def create_tables():
    """创建所有数据库表"""
    logger.info("开始创建数据库表...")

    try:
        # 导入所有模型以确保它们被注册
        from src.models import Base

        # 创建所有表
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("✅ 数据库表创建成功")
        return True

    except Exception as e:
        logger.error(f"❌ 数据库表创建失败: {e}")
        return False


# ✅ M3: 安全的硬编码 SQL 常量 - 所有索引创建语句都是预定义的，无用户输入
# 将 SQL 语句定义为模块级常量，明确标识这些是静态的、安全的 SQL
DATABASE_INDEX_DEFINITIONS = [
    # groups 表索引
    "CREATE INDEX IF NOT EXISTS idx_groups_id ON groups(id);",
    # warnings 表索引
    "CREATE INDEX IF NOT EXISTS idx_warnings_group_user ON warnings(group_id, user_id);",
    "CREATE INDEX IF NOT EXISTS idx_warnings_created_at ON warnings(created_at);",
    # spam_samples 表索引
    "CREATE INDEX IF NOT EXISTS idx_spam_samples_is_spam ON spam_samples(is_spam);",
    "CREATE INDEX IF NOT EXISTS idx_spam_samples_created_at ON spam_samples(created_at);",
    # audit_logs 表索引
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_group_id ON audit_logs(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_target_user ON audit_logs(target_user_id);",
]


async def create_indexes():
    """创建数据库索引以优化查询性能

    注意：此函数仅使用预定义的硬编码 SQL 常量（DATABASE_INDEX_DEFINITIONS），
    不接受任何用户输入，因此 text() 的使用是安全的。
    """
    logger.info("开始创建数据库索引...")

    try:
        async with engine.begin() as conn:
            # 使用预定义的安全 SQL 常量
            for index_sql in DATABASE_INDEX_DEFINITIONS:
                await conn.execute(text(index_sql))
                logger.debug(f"索引创建: {index_sql[:50]}...")

        logger.info("✅ 数据库索引创建成功")
        return True

    except Exception as e:
        logger.error(f"❌ 数据库索引创建失败: {e}")
        return False


async def check_database():
    """检查数据库连接和表状态"""
    logger.info("检查数据库连接...")

    try:
        async with engine.begin() as conn:
            # 检查连接
            result = await conn.execute(text("SELECT 1"))
            # ✅ L5: 替换 assert 为显式检查（assert 在优化编译时会被移除）
            if result.scalar() != 1:
                raise RuntimeError("数据库健康检查失败：连接测试返回错误结果")

            # 检查表是否存在
            tables_query = text("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
            """)

            result = await conn.execute(tables_query)
            tables = [row[0] for row in result.fetchall()]

            if tables:
                logger.info(f"✅ 数据库连接正常，已有表: {', '.join(tables)}")
            else:
                logger.info("✅ 数据库连接正常，但还没有表")

            return True

    except Exception as e:
        logger.error(f"❌ 数据库连接失败: {e}")
        return False


async def show_statistics():
    """显示数据库统计信息"""
    logger.info("获取数据库统计信息...")

    try:
        async with engine.begin() as conn:
            # 统计各表的行数
            stats_query = text("""
                SELECT
                    schemaname,
                    tablename,
                    n_live_tup as row_count
                FROM pg_stat_user_tables
                WHERE schemaname = 'public'
                ORDER BY tablename;
            """)

            result = await conn.execute(stats_query)
            stats = result.fetchall()

            if stats:
                logger.info("=== 数据库统计 ===")
                for schema, table, count in stats:
                    logger.info(f"  {table}: {count} 行")
            else:
                logger.info("暂无统计数据")

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="数据库迁移脚本")
    parser.add_argument("--check", action="store_true", help="检查数据库连接和状态")
    parser.add_argument("--create", action="store_true", help="创建数据库表")
    parser.add_argument("--indexes", action="store_true", help="创建数据库索引")
    parser.add_argument("--stats", action="store_true", help="显示数据库统计")

    args = parser.parse_args()

    # 初始化数据库连接
    await init_db()

    try:
        # 如果没有指定参数，执行完整迁移
        if not any([args.check, args.create, args.indexes, args.stats]):
            logger.info("=== 执行完整数据库迁移 ===")

            # 1. 检查数据库
            if not await check_database():
                logger.error("数据库连接失败，终止迁移")
                sys.exit(1)

            # 2. 创建表
            if not await create_tables():
                logger.error("表创建失败，终止迁移")
                sys.exit(1)

            # 3. 创建索引
            if not await create_indexes():
                logger.error("索引创建失败，终止迁移")
                sys.exit(1)

            # 4. 显示统计
            await show_statistics()

            logger.info("=== 数据库迁移完成 ===")

        else:
            # 执行指定的操作
            if args.check:
                await check_database()

            if args.create:
                await create_tables()

            if args.indexes:
                await create_indexes()

            if args.stats:
                await show_statistics()

    except Exception as e:
        logger.error(f"迁移过程中出错: {e}")
        sys.exit(1)

    finally:
        # 关闭数据库连接
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
