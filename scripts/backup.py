"""数据库备份脚本 - 自动备份和清理"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
import subprocess

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from src.core.config import settings


class DatabaseBackup:
    """数据库备份管理器"""

    def __init__(self, backup_dir: str = "backups"):
        """初始化备份管理器

        Args:
            backup_dir: 备份目录路径
        """
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(exist_ok=True)

    def create_backup(self) -> Path:
        """创建数据库备份

        Returns:
            备份文件路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backup_dir / f"backup_{timestamp}.sql"

        logger.info(f"开始备份数据库到: {backup_file}")

        try:
            # 使用 pg_dump 创建备份
            cmd = [
                "pg_dump",
                "-h", settings.db_host,
                "-p", str(settings.db_port),
                "-U", settings.db_user,
                "-d", settings.db_name,
                "-F", "p",  # 纯文本格式
                "-f", str(backup_file),
            ]

            # ✅ 修复：复制当前环境变量，避免丢失 PATH 等
            env = os.environ.copy()
            env["PGPASSWORD"] = settings.db_password

            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,  # 5 分钟超时
            )

            if result.returncode == 0:
                file_size = backup_file.stat().st_size / 1024 / 1024  # MB
                logger.info(f"✅ 备份成功: {backup_file.name} ({file_size:.2f} MB)")
                return backup_file
            else:
                logger.error(f"❌ 备份失败: {result.stderr}")
                if backup_file.exists():
                    backup_file.unlink()
                return None

        except subprocess.TimeoutExpired:
            logger.error("❌ 备份超时（5分钟）")
            return None
        except Exception as e:
            logger.error(f"❌ 备份失败: {e}")
            return None

    def restore_backup(self, backup_file: Path) -> bool:
        """恢复数据库备份

        Args:
            backup_file: 备份文件路径

        Returns:
            是否恢复成功
        """
        if not backup_file.exists():
            logger.error(f"❌ 备份文件不存在: {backup_file}")
            return False

        logger.info(f"开始恢复数据库从: {backup_file}")

        try:
            # 使用 psql 恢复备份
            cmd = [
                "psql",
                "-h", settings.db_host,
                "-p", str(settings.db_port),
                "-U", settings.db_user,
                "-d", settings.db_name,
                "-f", str(backup_file),
            ]

            # ✅ 修复：复制当前环境变量，避免丢失 PATH 等
            env = os.environ.copy()
            env["PGPASSWORD"] = settings.db_password

            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                logger.info("✅ 恢复成功")
                return True
            else:
                logger.error(f"❌ 恢复失败: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"❌ 恢复失败: {e}")
            return False

    def list_backups(self) -> list[Path]:
        """列出所有备份文件

        Returns:
            备份文件列表（按时间倒序）
        """
        backups = sorted(
            self.backup_dir.glob("backup_*.sql"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return backups

    def clean_old_backups(self, keep_days: int = 7, keep_count: int = 10) -> int:
        """清理旧备份

        Args:
            keep_days: 保留最近 N 天的备份
            keep_count: 至少保留 N 个最新备份

        Returns:
            删除的备份数量
        """
        logger.info(f"开始清理旧备份（保留 {keep_days} 天，至少 {keep_count} 个）")

        backups = self.list_backups()

        if len(backups) <= keep_count:
            logger.info("备份数量未超过最小保留数量，跳过清理")
            return 0

        cutoff_time = datetime.now() - timedelta(days=keep_days)
        deleted_count = 0

        for backup in backups[keep_count:]:  # 跳过最新的 keep_count 个
            backup_time = datetime.fromtimestamp(backup.stat().st_mtime)

            if backup_time < cutoff_time:
                try:
                    backup.unlink()
                    deleted_count += 1
                    logger.info(f"删除旧备份: {backup.name}")
                except Exception as e:
                    logger.error(f"删除备份失败 {backup.name}: {e}")

        logger.info(f"✅ 清理完成，删除了 {deleted_count} 个旧备份")
        return deleted_count

    def show_backups(self):
        """显示所有备份信息"""
        backups = self.list_backups()

        if not backups:
            logger.info("暂无备份文件")
            return

        logger.info(f"=== 备份列表（共 {len(backups)} 个）===")

        for i, backup in enumerate(backups, 1):
            size_mb = backup.stat().st_size / 1024 / 1024
            mtime = datetime.fromtimestamp(backup.stat().st_mtime)
            age = datetime.now() - mtime

            age_str = f"{age.days}天前" if age.days > 0 else f"{age.seconds // 3600}小时前"

            logger.info(
                f"{i}. {backup.name} - {size_mb:.2f} MB - {age_str} ({mtime.strftime('%Y-%m-%d %H:%M:%S')})"
            )


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="数据库备份脚本")
    parser.add_argument("--backup", action="store_true", help="创建备份")
    parser.add_argument("--restore", type=str, help="恢复备份（指定备份文件名）")
    parser.add_argument("--list", action="store_true", help="列出所有备份")
    parser.add_argument("--clean", action="store_true", help="清理旧备份")
    parser.add_argument("--keep-days", type=int, default=7, help="保留最近 N 天的备份（默认 7）")
    parser.add_argument("--keep-count", type=int, default=10, help="至少保留 N 个最新备份（默认 10）")

    args = parser.parse_args()

    backup_manager = DatabaseBackup()

    if args.backup:
        # 创建备份
        backup_file = backup_manager.create_backup()
        if backup_file:
            sys.exit(0)
        else:
            sys.exit(1)

    elif args.restore:
        # 恢复备份
        backup_file = backup_manager.backup_dir / args.restore
        success = backup_manager.restore_backup(backup_file)
        sys.exit(0 if success else 1)

    elif args.list:
        # 列出备份
        backup_manager.show_backups()

    elif args.clean:
        # 清理旧备份
        backup_manager.clean_old_backups(
            keep_days=args.keep_days,
            keep_count=args.keep_count,
        )

    else:
        # 默认：创建备份并清理旧备份
        logger.info("=== 执行自动备份和清理 ===")

        # 1. 创建备份
        backup_file = backup_manager.create_backup()
        if not backup_file:
            logger.error("备份失败，终止操作")
            sys.exit(1)

        # 2. 清理旧备份
        backup_manager.clean_old_backups(
            keep_days=args.keep_days,
            keep_count=args.keep_count,
        )

        logger.info("=== 备份和清理完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
