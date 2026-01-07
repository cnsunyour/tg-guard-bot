"""数据库版本化迁移脚本

此脚本用于管理数据库 schema 的版本化更新。
每个迁移文件按顺序编号，系统会跟踪已应用的迁移。
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from sqlalchemy import text

from src.core.database import close_db, engine, init_db


async def create_migrations_table():
    """创建迁移记录表"""
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    description TEXT
                )
            """
                )
            )
        logger.info("✅ 迁移记录表已就绪")
        return True
    except Exception as e:
        logger.error(f"❌ 创建迁移记录表失败: {e}")
        return False


async def get_applied_migrations():
    """获取已应用的迁移列表"""
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT version FROM schema_migrations ORDER BY version"))
            return {row[0] for row in result.fetchall()}
    except Exception as e:
        logger.error(f"获取迁移记录失败: {e}")
        return set()


async def apply_migration(migration_file: Path):
    """应用单个迁移文件"""
    version = migration_file.stem
    logger.info(f"应用迁移: {version}")

    try:
        # 读取 SQL 文件
        sql_content = migration_file.read_text(encoding="utf-8")

        # 执行迁移
        async with engine.begin() as conn:
            # 执行 SQL（处理多个语句）
            for statement in sql_content.split(";"):
                statement = statement.strip()
                if statement and not statement.startswith("--"):
                    await conn.execute(text(statement))

            # 记录迁移
            await conn.execute(
                text(
                    """
                INSERT INTO schema_migrations (version, description)
                VALUES (:version, :description)
            """
                ),
                {
                    "version": version,
                    "description": f"Applied migration from {migration_file.name}",
                },
            )

        logger.info(f"✅ 迁移 {version} 应用成功")
        return True

    except Exception as e:
        logger.error(f"❌ 迁移 {version} 应用失败: {e}")
        return False


async def run_migrations(migrations_dir: Path):
    """运行所有待处理的迁移"""
    # 确保迁移记录表存在
    if not await create_migrations_table():
        return False

    # 获取已应用的迁移
    applied = await get_applied_migrations()
    logger.info(f"已应用的迁移: {len(applied)} 个")

    # 查找所有迁移文件
    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        logger.info("未找到待处理的迁移文件")
        return True

    # 应用未执行的迁移
    pending_migrations = [f for f in migration_files if f.stem not in applied]

    if not pending_migrations:
        logger.info("✅ 所有迁移已应用，无需执行")
        return True

    logger.info(f"发现 {len(pending_migrations)} 个待处理的迁移")

    success = True
    for migration_file in pending_migrations:
        if not await apply_migration(migration_file):
            success = False
            break

    return success


async def list_migrations(migrations_dir: Path):
    """列出所有迁移及其状态"""
    # 确保迁移记录表存在
    await create_migrations_table()

    # 获取已应用的迁移
    applied = await get_applied_migrations()

    # 查找所有迁移文件
    migration_files = sorted(migrations_dir.glob("*.sql"))

    logger.info("=== 迁移状态 ===")
    for migration_file in migration_files:
        version = migration_file.stem
        status = "✅ 已应用" if version in applied else "⏳ 待处理"
        logger.info(f"{status} - {migration_file.name}")

    if not migration_files:
        logger.info("未找到任何迁移文件")


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="数据库版本化迁移工具")
    parser.add_argument("--run", action="store_true", help="运行所有待处理的迁移")
    parser.add_argument("--list", action="store_true", help="列出所有迁移及状态")
    parser.add_argument(
        "--migrations-dir",
        type=str,
        default="migrations",
        help="迁移文件目录（默认: migrations）",
    )

    args = parser.parse_args()

    # 确定迁移目录
    project_root = Path(__file__).parent.parent
    migrations_dir = project_root / args.migrations_dir

    if not migrations_dir.exists():
        logger.error(f"迁移目录不存在: {migrations_dir}")
        sys.exit(1)

    # 初始化数据库连接
    await init_db()

    try:
        if args.list:
            await list_migrations(migrations_dir)
        elif args.run:
            logger.info("=== 开始数据库迁移 ===")
            if await run_migrations(migrations_dir):
                logger.info("=== 数据库迁移完成 ===")
            else:
                logger.error("=== 数据库迁移失败 ===")
                sys.exit(1)
        else:
            # 默认行为：列出状态
            await list_migrations(migrations_dir)
            logger.info("\n使用 --run 参数执行迁移")

    except Exception as e:
        logger.error(f"迁移过程中出错: {e}")
        sys.exit(1)

    finally:
        # 关闭数据库连接
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
