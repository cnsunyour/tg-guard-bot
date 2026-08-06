"""数据库迁移脚本 - 初始化数据库表"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from sqlalchemy import text

from src.core.database import close_db, engine, init_db


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
                    relname as tablename,
                    n_live_tup as row_count
                FROM pg_stat_user_tables
                WHERE schemaname = 'public'
                ORDER BY relname;
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


async def execute_migrations():
    """执行 migrations 目录中的 SQL 迁移文件"""
    logger.info("开始执行 SQL 迁移...")

    # 获取 migrations 目录
    migrations_dir = Path(__file__).parent.parent / "migrations"

    if not migrations_dir.exists():
        logger.warning(f"migrations 目录不存在: {migrations_dir}")
        return True

    # 获取所有 .sql 文件并排序
    sql_files = sorted(migrations_dir.glob("*.sql"))

    if not sql_files:
        logger.info("没有找到 SQL 迁移文件")
        return True

    # 创建 schema_migrations 表来跟踪已执行的迁移
    # 兼容现有表结构：version, applied_at, description
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    description TEXT
                );
            """)
            )
    except Exception as e:
        logger.error(f"创建 schema_migrations 表失败: {e}")
        return False

    # 执行每个迁移文件
    success_count = 0
    skip_count = 0
    error_count = 0

    for sql_file in sql_files:
        filename = sql_file.name

        try:
            async with engine.begin() as conn:
                # 检查是否已执行（使用 version 字段存储文件名）
                result = await conn.execute(
                    text("SELECT COUNT(*) FROM schema_migrations WHERE version = :version"),
                    {"version": filename},
                )
                count = result.scalar()

                if count > 0:
                    logger.debug(f"⏭️  跳过已执行的迁移: {filename}")
                    skip_count += 1
                    continue

                # 读取并执行 SQL 文件
                logger.info(f"🔄 执行迁移: {filename}")
                sql_content = sql_file.read_text(encoding="utf-8")

                # 提取描述（从文件的第二行注释中）
                description = ""
                for line in sql_content.split("\n"):
                    if line.startswith("-- Description:"):
                        description = line.replace("-- Description:", "").strip()
                        break

                # 执行 SQL（可能包含多个语句）
                # 先剔除独立注释行：旧逻辑按分号切分后，会把“注释行 + 紧随的首条 SQL”
                # 粘在同一段并因 startswith("--") 整体丢弃，导致带注释头的迁移首条语句被静默跳过。
                executable_sql = "\n".join(
                    line
                    for line in sql_content.splitlines()
                    if not line.lstrip().startswith("--")
                )
                for statement in executable_sql.split(";"):
                    statement = statement.strip()
                    if statement:
                        await conn.execute(text(statement))

                # 记录已执行的迁移
                await conn.execute(
                    text(
                        """
                    INSERT INTO schema_migrations (version, description)
                    VALUES (:version, :description)
                """
                    ),
                    {"version": filename, "description": description},
                )

                logger.info(f"✅ 迁移成功: {filename}")
                success_count += 1

        except Exception as e:
            logger.error(f"❌ 迁移失败 {filename}: {e}")
            error_count += 1
            # 继续执行其他迁移，不中断

    # 总结
    logger.info(
        f"=== 迁移执行完成: 成功 {success_count}, 跳过 {skip_count}, 失败 {error_count} ==="
    )

    return error_count == 0


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="数据库迁移脚本")
    parser.add_argument("--check", action="store_true", help="检查数据库连接和状态")
    parser.add_argument("--create", action="store_true", help="创建数据库表")
    parser.add_argument("--indexes", action="store_true", help="创建数据库索引")
    parser.add_argument("--migrations", action="store_true", help="执行 SQL 迁移文件")
    parser.add_argument("--stats", action="store_true", help="显示数据库统计")

    args = parser.parse_args()

    # 初始化数据库连接
    await init_db()

    try:
        # 如果没有指定参数，执行完整迁移
        if not any([args.check, args.create, args.indexes, args.migrations, args.stats]):
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

            # 4. 执行 SQL 迁移
            if not await execute_migrations():
                logger.error("SQL 迁移执行失败，终止迁移")
                sys.exit(1)

            # 5. 显示统计
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

            if args.migrations:
                await execute_migrations()

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
