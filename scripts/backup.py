"""数据库备份脚本 - PostgreSQL + Redis 自动备份与 GFS 轮转"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from subprocess import TimeoutExpired, run

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.core.config import settings


class BackupType(Enum):
    """备份类型"""

    DAILY = "daily"  # 每日备份（保留 7 天）
    WEEKLY = "weekly"  # 每周备份（保留 4 周）
    MONTHLY = "monthly"  # 月备份（保留 6 个月）


class BackupManager:
    """统一备份管理器（PostgreSQL + Redis）

    实现 GFS (Grandfather-Father-Son) 轮转策略：
    - Daily: 保留最近 7 天的每日备份
    - Weekly: 保留最近 4 周的每周备份（周日）
    - Monthly: 保留最近 6 个月的月备份（每月第一个周日）
    """

    def __init__(self, backup_dir: str = "backups"):
        """初始化备份管理器

        Args:
            backup_dir: 备份根目录
        """
        self.backup_dir = Path(backup_dir)
        self.daily_dir = self.backup_dir / "daily"
        self.weekly_dir = self.backup_dir / "weekly"
        self.monthly_dir = self.backup_dir / "monthly"

        # 创建备份目录
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.weekly_dir.mkdir(parents=True, exist_ok=True)
        self.monthly_dir.mkdir(parents=True, exist_ok=True)

    def backup_postgres(self, backup_type: BackupType) -> Path | None:
        """备份 PostgreSQL 数据库

        Args:
            backup_type: 备份类型（daily/weekly/monthly）

        Returns:
            备份文件路径，失败返回 None
        """
        timestamp = datetime.now().strftime("%Y%m%d")

        # 确定目标目录和文件后缀
        if backup_type == BackupType.DAILY:
            target_dir = self.daily_dir
            suffix = ""
        elif backup_type == BackupType.WEEKLY:
            target_dir = self.weekly_dir
            suffix = "_weekly"
        else:  # MONTHLY
            target_dir = self.monthly_dir
            suffix = "_monthly"

        backup_file = target_dir / f"postgres_{timestamp}{suffix}.sql"

        logger.info(f"开始备份 PostgreSQL 到: {backup_file}")

        try:
            # 使用 Docker exec 执行 pg_dump
            cmd = [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={settings.db_password}",
                "tg-guard-postgres",
                "pg_dump",
                "-U",
                settings.db_user,
                "-d",
                settings.db_name,
                "-F",
                "p",  # 纯文本格式
            ]

            result = run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 分钟超时
            )

            # 将输出写入备份文件
            if result.returncode == 0:
                backup_file.write_text(result.stdout, encoding="utf-8")

            if result.returncode == 0:
                file_size = backup_file.stat().st_size / 1024 / 1024  # MB
                logger.info(f"✅ PostgreSQL 备份成功: {backup_file.name} ({file_size:.2f} MB)")
                return backup_file
            else:
                logger.error(f"❌ PostgreSQL 备份失败: {result.stderr}")
                if backup_file.exists():
                    backup_file.unlink()
                return None

        except TimeoutExpired:
            logger.error("❌ PostgreSQL 备份超时（5分钟）")
            if backup_file.exists():
                backup_file.unlink()
            return None
        except Exception as e:
            logger.error(f"❌ PostgreSQL 备份失败: {e}")
            if backup_file.exists():
                backup_file.unlink()
            return None

    def backup_redis(self, backup_type: BackupType) -> Path | None:
        """备份 Redis 数据

        通过 Docker 容器触发 BGSAVE 并复制 dump.rdb 文件

        Args:
            backup_type: 备份类型（daily/weekly/monthly）

        Returns:
            备份文件路径，失败返回 None
        """
        timestamp = datetime.now().strftime("%Y%m%d")

        # 确定目标目录和文件后缀
        if backup_type == BackupType.DAILY:
            target_dir = self.daily_dir
            suffix = ""
        elif backup_type == BackupType.WEEKLY:
            target_dir = self.weekly_dir
            suffix = "_weekly"
        else:  # MONTHLY
            target_dir = self.monthly_dir
            suffix = "_monthly"

        backup_file = target_dir / f"redis_{timestamp}{suffix}.rdb"

        logger.info(f"开始备份 Redis 到: {backup_file}")

        try:
            # 通过宿主 env 传 REDISCLI_AUTH（避免密码出现在宿主 argv / docker inspect）；
            # `docker exec -e REDISCLI_AUTH`（无值）从当前 env 继承并注入容器
            redis_cli_env = os.environ.copy()
            redis_cli_env["REDISCLI_AUTH"] = settings.redis_password or ""

            # 1. 触发 BGSAVE
            logger.debug("触发 Redis BGSAVE...")
            result = run(
                [
                    "docker",
                    "exec",
                    "-e",
                    "REDISCLI_AUTH",
                    "tg-guard-redis",
                    "redis-cli",
                    "BGSAVE",
                ],
                env=redis_cli_env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                logger.error(f"❌ 触发 Redis BGSAVE 失败: {result.stderr}")
                return None

            # 2. 等待 BGSAVE 完成
            logger.debug("等待 Redis BGSAVE 完成...")
            max_wait = 60  # 最多等待 60 秒
            start_time = time.time()
            last_save_time = None

            while time.time() - start_time < max_wait:
                result = run(
                    [
                        "docker",
                        "exec",
                        "-e",
                        "REDISCLI_AUTH",
                        "tg-guard-redis",
                        "redis-cli",
                        "LASTSAVE",
                    ],
                    env=redis_cli_env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

                if result.returncode == 0:
                    current_save_time = int(result.stdout.strip())

                    if last_save_time is None:
                        last_save_time = current_save_time
                    elif current_save_time > last_save_time:
                        logger.debug(f"BGSAVE 完成，时间戳: {current_save_time}")
                        break

                time.sleep(1)
            else:
                logger.warning("⚠️ BGSAVE 等待超时，尝试继续复制文件")

            # 3. 复制 dump.rdb 到备份目录
            logger.debug("复制 dump.rdb 文件...")
            result = run(
                [
                    "docker",
                    "cp",
                    "tg-guard-redis:/data/dump.rdb",
                    str(backup_file),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                if backup_file.exists():
                    file_size = backup_file.stat().st_size / 1024 / 1024  # MB
                    logger.info(f"✅ Redis 备份成功: {backup_file.name} ({file_size:.2f} MB)")
                    return backup_file
                else:
                    logger.error("❌ Redis 备份文件不存在")
                    return None
            else:
                logger.error(f"❌ 复制 Redis 备份失败: {result.stderr}")
                return None

        except TimeoutExpired:
            logger.error("❌ Redis 备份超时")
            return None
        except Exception as e:
            logger.error(f"❌ Redis 备份失败: {e}")
            return None

    def promote_to_weekly(self) -> dict:
        """提升当日备份为每周备份

        在周日执行时，将 daily 备份复制到 weekly 目录

        Returns:
            {"promoted": int, "errors": list} 统计信息
        """
        today = datetime.now()

        # 只在周日执行
        if today.weekday() != 6:  # 6 = 周日
            logger.debug(f"今天是 {today.strftime('%A')}，不是周日，跳过 weekly 提升")
            return {"promoted": 0, "errors": []}

        logger.info("今天是周日，开始提升当日备份为 weekly 备份...")

        timestamp = today.strftime("%Y%m%d")
        promoted_count = 0
        errors = []

        # 提升 PostgreSQL 备份
        daily_pg = self.daily_dir / f"postgres_{timestamp}.sql"
        weekly_pg = self.weekly_dir / f"postgres_{timestamp}_weekly.sql"

        if daily_pg.exists():
            try:
                import shutil

                shutil.copy2(daily_pg, weekly_pg)
                logger.info(f"✅ 提升 PostgreSQL 备份: {weekly_pg.name}")
                promoted_count += 1
            except Exception as e:
                error_msg = f"提升 PostgreSQL 备份失败: {e}"
                logger.error(f"❌ {error_msg}")
                errors.append(error_msg)
        else:
            logger.warning(f"⚠️ 当日 PostgreSQL 备份不存在: {daily_pg}")

        # 提升 Redis 备份
        daily_redis = self.daily_dir / f"redis_{timestamp}.rdb"
        weekly_redis = self.weekly_dir / f"redis_{timestamp}_weekly.rdb"

        if daily_redis.exists():
            try:
                import shutil

                shutil.copy2(daily_redis, weekly_redis)
                logger.info(f"✅ 提升 Redis 备份: {weekly_redis.name}")
                promoted_count += 1
            except Exception as e:
                error_msg = f"提升 Redis 备份失败: {e}"
                logger.error(f"❌ {error_msg}")
                errors.append(error_msg)
        else:
            logger.warning(f"⚠️ 当日 Redis 备份不存在: {daily_redis}")

        logger.info(f"Weekly 提升完成，共提升 {promoted_count} 个备份")
        return {"promoted": promoted_count, "errors": errors}

    def promote_to_monthly(self) -> dict:
        """提升当日备份为月备份

        在每月第一个周日执行时，将 daily 备份复制到 monthly 目录

        Returns:
            {"promoted": int, "errors": list} 统计信息
        """
        today = datetime.now()

        # 判断是否是每月第一个周日
        if today.weekday() != 6:  # 6 = 周日
            logger.debug(f"今天是 {today.strftime('%A')}，不是周日，跳过 monthly 提升")
            return {"promoted": 0, "errors": []}

        # 检查是否是本月第一个周日（日期 <= 7）
        if today.day > 7:
            logger.debug(f"今天是本月第 {today.day} 天，不是第一个周日，跳过 monthly 提升")
            return {"promoted": 0, "errors": []}

        logger.info("今天是本月第一个周日，开始提升当日备份为 monthly 备份...")

        timestamp = today.strftime("%Y%m%d")
        promoted_count = 0
        errors = []

        # 提升 PostgreSQL 备份
        daily_pg = self.daily_dir / f"postgres_{timestamp}.sql"
        monthly_pg = self.monthly_dir / f"postgres_{timestamp}_monthly.sql"

        if daily_pg.exists():
            try:
                import shutil

                shutil.copy2(daily_pg, monthly_pg)
                logger.info(f"✅ 提升 PostgreSQL 月备份: {monthly_pg.name}")
                promoted_count += 1
            except Exception as e:
                error_msg = f"提升 PostgreSQL 月备份失败: {e}"
                logger.error(f"❌ {error_msg}")
                errors.append(error_msg)
        else:
            logger.warning(f"⚠️ 当日 PostgreSQL 备份不存在: {daily_pg}")

        # 提升 Redis 备份
        daily_redis = self.daily_dir / f"redis_{timestamp}.rdb"
        monthly_redis = self.monthly_dir / f"redis_{timestamp}_monthly.rdb"

        if daily_redis.exists():
            try:
                import shutil

                shutil.copy2(daily_redis, monthly_redis)
                logger.info(f"✅ 提升 Redis 月备份: {monthly_redis.name}")
                promoted_count += 1
            except Exception as e:
                error_msg = f"提升 Redis 月备份失败: {e}"
                logger.error(f"❌ {error_msg}")
                errors.append(error_msg)
        else:
            logger.warning(f"⚠️ 当日 Redis 备份不存在: {daily_redis}")

        logger.info(f"Monthly 提升完成，共提升 {promoted_count} 个备份")
        return {"promoted": promoted_count, "errors": errors}

    def cleanup_expired(self) -> dict:
        """清理过期备份

        - daily/: 删除超过 7 天的备份
        - weekly/: 删除超过 28 天（4 周）的备份
        - monthly/: 删除超过 180 天（6 个月）的备份

        Returns:
            {"deleted_daily": int, "deleted_weekly": int, "deleted_monthly": int} 统计信息
        """
        logger.info("开始清理过期备份...")

        now = datetime.now()
        deleted_daily = 0
        deleted_weekly = 0
        deleted_monthly = 0

        # 清理 daily 备份（保留 7 天）
        daily_cutoff = now - timedelta(days=7)
        for backup_file in self.daily_dir.glob("*"):
            if not backup_file.is_file():
                continue

            file_time = datetime.fromtimestamp(backup_file.stat().st_mtime)
            if file_time < daily_cutoff:
                try:
                    backup_file.unlink()
                    deleted_daily += 1
                    logger.info(f"删除过期 daily 备份: {backup_file.name}")
                except Exception as e:
                    logger.error(f"删除 {backup_file.name} 失败: {e}")

        # 清理 weekly 备份（保留 28 天 = 4 周）
        weekly_cutoff = now - timedelta(days=28)
        for backup_file in self.weekly_dir.glob("*"):
            if not backup_file.is_file():
                continue

            file_time = datetime.fromtimestamp(backup_file.stat().st_mtime)
            if file_time < weekly_cutoff:
                try:
                    backup_file.unlink()
                    deleted_weekly += 1
                    logger.info(f"删除过期 weekly 备份: {backup_file.name}")
                except Exception as e:
                    logger.error(f"删除 {backup_file.name} 失败: {e}")

        # 清理 monthly 备份（保留 180 天 = 6 个月）
        monthly_cutoff = now - timedelta(days=180)
        for backup_file in self.monthly_dir.glob("*"):
            if not backup_file.is_file():
                continue

            file_time = datetime.fromtimestamp(backup_file.stat().st_mtime)
            if file_time < monthly_cutoff:
                try:
                    backup_file.unlink()
                    deleted_monthly += 1
                    logger.info(f"删除过期 monthly 备份: {backup_file.name}")
                except Exception as e:
                    logger.error(f"删除 {backup_file.name} 失败: {e}")

        logger.info(
            f"✅ 清理完成，删除 {deleted_daily} 个 daily 备份，"
            f"{deleted_weekly} 个 weekly 备份，{deleted_monthly} 个 monthly 备份"
        )
        return {
            "deleted_daily": deleted_daily,
            "deleted_weekly": deleted_weekly,
            "deleted_monthly": deleted_monthly,
        }

    def run_backup(self) -> dict:
        """执行完整备份流程

        1. 创建 daily 备份（PostgreSQL + Redis）
        2. 如果是周日，提升为 weekly 备份
        3. 如果是每月第一个周日，提升为 monthly 备份
        4. 清理过期备份

        Returns:
            备份统计信息字典
        """
        logger.info("=== 开始执行自动备份流程 ===")
        start_time = time.time()

        stats = {
            "success": True,
            "postgres_backup": None,
            "redis_backup": None,
            "promoted_weekly": 0,
            "promoted_monthly": 0,
            "deleted_daily": 0,
            "deleted_weekly": 0,
            "deleted_monthly": 0,
            "errors": [],
        }

        # 1. 创建 PostgreSQL 备份
        pg_backup = self.backup_postgres(BackupType.DAILY)
        if pg_backup:
            stats["postgres_backup"] = str(pg_backup)
        else:
            stats["success"] = False
            stats["errors"].append("PostgreSQL 备份失败")

        # 2. 创建 Redis 备份
        redis_backup = self.backup_redis(BackupType.DAILY)
        if redis_backup:
            stats["redis_backup"] = str(redis_backup)
        else:
            stats["success"] = False
            stats["errors"].append("Redis 备份失败")

        # 3. 提升为 weekly（如果是周日）
        promote_weekly_result = self.promote_to_weekly()
        stats["promoted_weekly"] = promote_weekly_result["promoted"]
        stats["errors"].extend(promote_weekly_result["errors"])

        # 4. 提升为 monthly（如果是每月第一个周日）
        promote_monthly_result = self.promote_to_monthly()
        stats["promoted_monthly"] = promote_monthly_result["promoted"]
        stats["errors"].extend(promote_monthly_result["errors"])

        # 5. 清理过期备份
        cleanup_result = self.cleanup_expired()
        stats["deleted_daily"] = cleanup_result["deleted_daily"]
        stats["deleted_weekly"] = cleanup_result["deleted_weekly"]
        stats["deleted_monthly"] = cleanup_result["deleted_monthly"]

        # 统计耗时
        elapsed = time.time() - start_time
        logger.info(f"=== 备份流程完成，耗时 {elapsed:.2f} 秒 ===")

        # 打印总结
        if stats["success"]:
            logger.info("✅ 备份成功")
        else:
            logger.error(f"❌ 备份失败: {', '.join(stats['errors'])}")

        return stats

    def list_backups(self) -> dict:
        """列出所有备份文件

        Returns:
            {"daily": list, "weekly": list, "monthly": list} 备份文件列表
        """
        daily_backups = sorted(
            [f for f in self.daily_dir.glob("*") if f.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        weekly_backups = sorted(
            [f for f in self.weekly_dir.glob("*") if f.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        monthly_backups = sorted(
            [f for f in self.monthly_dir.glob("*") if f.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        return {"daily": daily_backups, "weekly": weekly_backups, "monthly": monthly_backups}

    def show_backups(self):
        """显示所有备份信息（格式化输出）"""
        backups = self.list_backups()

        # 显示 daily 备份
        logger.info(f"=== Daily 备份（保留 7 天，共 {len(backups['daily'])} 个）===")
        for i, backup in enumerate(backups["daily"], 1):
            self._print_backup_info(i, backup)

        logger.info("")

        # 显示 weekly 备份
        logger.info(f"=== Weekly 备份（保留 4 周，共 {len(backups['weekly'])} 个）===")
        for i, backup in enumerate(backups["weekly"], 1):
            self._print_backup_info(i, backup)

        logger.info("")

        # 显示 monthly 备份
        logger.info(f"=== Monthly 备份（保留 6 个月，共 {len(backups['monthly'])} 个）===")
        for i, backup in enumerate(backups["monthly"], 1):
            self._print_backup_info(i, backup)

    def _print_backup_info(self, index: int, backup: Path):
        """打印单个备份文件信息"""
        size_mb = backup.stat().st_size / 1024 / 1024
        mtime = datetime.fromtimestamp(backup.stat().st_mtime)
        age = datetime.now() - mtime

        if age.days > 0:
            age_str = f"{age.days}天前"
        elif age.seconds >= 3600:
            age_str = f"{age.seconds // 3600}小时前"
        else:
            age_str = f"{age.seconds // 60}分钟前"

        logger.info(
            f"{index}. {backup.name} - {size_mb:.2f} MB - {age_str} ({mtime.strftime('%Y-%m-%d %H:%M:%S')})"
        )

    def restore_postgres(self, backup_file: Path) -> bool:
        """恢复 PostgreSQL 备份

        Args:
            backup_file: 备份文件路径

        Returns:
            是否恢复成功
        """
        if not backup_file.exists():
            logger.error(f"❌ 备份文件不存在: {backup_file}")
            return False

        logger.info(f"开始恢复 PostgreSQL 从: {backup_file}")

        try:
            # 使用 Docker exec 执行 psql 恢复
            # 读取备份文件内容
            backup_content = backup_file.read_text(encoding="utf-8")

            cmd = [
                "docker",
                "exec",
                "-i",
                "-e",
                f"PGPASSWORD={settings.db_password}",
                "tg-guard-postgres",
                "psql",
                "-U",
                settings.db_user,
                "-d",
                settings.db_name,
            ]

            result = run(
                cmd,
                input=backup_content,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                logger.info("✅ PostgreSQL 恢复成功")
                return True
            else:
                logger.error(f"❌ PostgreSQL 恢复失败: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"❌ PostgreSQL 恢复失败: {e}")
            return False

    def restore_redis(self, backup_file: Path) -> bool:
        """恢复 Redis 备份

        注意：需要停止 Redis 服务后才能恢复

        Args:
            backup_file: 备份文件路径（.rdb）

        Returns:
            是否恢复成功
        """
        if not backup_file.exists():
            logger.error(f"❌ 备份文件不存在: {backup_file}")
            return False

        logger.warning("⚠️ Redis 恢复需要停止服务，请确保已停止 tg-guard-redis 容器")
        logger.info(f"开始恢复 Redis 从: {backup_file}")

        try:
            # 复制备份文件到 Redis 数据目录
            result = run(
                [
                    "docker",
                    "cp",
                    str(backup_file),
                    "tg-guard-redis:/data/dump.rdb",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                logger.info("✅ Redis 备份文件已复制，请重启 tg-guard-redis 容器")
                logger.info("命令: docker-compose restart redis")
                return True
            else:
                logger.error(f"❌ Redis 恢复失败: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"❌ Redis 恢复失败: {e}")
            return False


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="PostgreSQL + Redis 自动备份脚本（GFS 轮转）")
    parser.add_argument("--backup", action="store_true", help="执行完整备份流程")
    parser.add_argument("--postgres", action="store_true", help="仅备份 PostgreSQL")
    parser.add_argument("--redis", action="store_true", help="仅备份 Redis")
    parser.add_argument("--list", action="store_true", help="列出所有备份")
    parser.add_argument("--restore-postgres", type=str, metavar="FILE", help="恢复 PostgreSQL 备份")
    parser.add_argument("--restore-redis", type=str, metavar="FILE", help="恢复 Redis 备份")
    parser.add_argument("--cleanup", action="store_true", help="仅清理过期备份")

    args = parser.parse_args()

    backup_manager = BackupManager()

    # 命令路由
    if args.postgres:
        # 仅备份 PostgreSQL
        result = backup_manager.backup_postgres(BackupType.DAILY)
        sys.exit(0 if result else 1)

    elif args.redis:
        # 仅备份 Redis
        result = backup_manager.backup_redis(BackupType.DAILY)
        sys.exit(0 if result else 1)

    elif args.list:
        # 列出所有备份
        backup_manager.show_backups()
        sys.exit(0)

    elif args.restore_postgres:
        # 恢复 PostgreSQL
        backup_file = Path(args.restore_postgres)
        success = backup_manager.restore_postgres(backup_file)
        sys.exit(0 if success else 1)

    elif args.restore_redis:
        # 恢复 Redis
        backup_file = Path(args.restore_redis)
        success = backup_manager.restore_redis(backup_file)
        sys.exit(0 if success else 1)

    elif args.cleanup:
        # 仅清理过期备份
        backup_manager.cleanup_expired()
        sys.exit(0)

    elif args.backup:
        # 执行完整备份流程
        stats = backup_manager.run_backup()
        sys.exit(0 if stats["success"] else 1)

    else:
        # 默认：执行完整备份流程
        stats = backup_manager.run_backup()
        sys.exit(0 if stats["success"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
