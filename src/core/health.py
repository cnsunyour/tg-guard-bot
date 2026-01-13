"""健康检查和性能监控模块"""

import time
from datetime import datetime
from typing import Any

import psutil
from loguru import logger
from sqlalchemy import text

from src.core.database import engine
from src.core.redis import get_redis


class HealthChecker:
    """健康检查器"""

    def __init__(self):
        """初始化健康检查器"""
        self.start_time = time.time()
        self.check_count = 0

    async def check_database(self) -> dict[str, Any]:
        """检查数据库连接

        Returns:
            {healthy: bool, latency_ms: float, error: str}
        """
        start = time.time()

        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))

            latency = (time.time() - start) * 1000

            return {
                "healthy": True,
                "latency_ms": round(latency, 2),
                "error": None,
            }

        except Exception as e:
            latency = (time.time() - start) * 1000

            return {
                "healthy": False,
                "latency_ms": round(latency, 2),
                "error": str(e),
            }

    async def check_redis(self) -> dict[str, Any]:
        """检查 Redis 连接

        Returns:
            {healthy: bool, latency_ms: float, error: str}
        """
        start = time.time()

        try:
            # ✅ P0-2: get_redis() 是同步函数，不需要 await
            redis = get_redis()
            await redis.ping()

            latency = (time.time() - start) * 1000

            return {
                "healthy": True,
                "latency_ms": round(latency, 2),
                "error": None,
            }

        except Exception as e:
            latency = (time.time() - start) * 1000

            return {
                "healthy": False,
                "latency_ms": round(latency, 2),
                "error": str(e),
            }

    def get_system_metrics(self) -> dict[str, Any]:
        """获取系统性能指标

        Returns:
            系统指标字典
        """
        try:
            # CPU 使用率
            cpu_percent = psutil.cpu_percent(interval=0.1)

            # 内存使用
            memory = psutil.virtual_memory()

            # 磁盘使用
            disk = psutil.disk_usage("/")

            return {
                "cpu": {
                    "percent": cpu_percent,
                    "count": psutil.cpu_count(),
                },
                "memory": {
                    "total_mb": round(memory.total / 1024 / 1024, 2),
                    "used_mb": round(memory.used / 1024 / 1024, 2),
                    "percent": memory.percent,
                },
                "disk": {
                    "total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
                    "used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
                    "percent": disk.percent,
                },
            }

        except Exception as e:
            logger.error(f"获取系统指标失败: {e}")
            return {}

    def get_uptime(self) -> dict[str, Any]:
        """获取运行时间

        Returns:
            运行时间信息
        """
        uptime_seconds = time.time() - self.start_time

        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        seconds = int(uptime_seconds % 60)

        return {
            "seconds": int(uptime_seconds),
            "formatted": f"{days}天 {hours}小时 {minutes}分钟 {seconds}秒",
            "started_at": datetime.fromtimestamp(self.start_time).isoformat(),
        }

    async def full_check(self) -> dict[str, Any]:
        """执行完整健康检查

        Returns:
            完整的健康检查报告
        """
        self.check_count += 1

        db_check = await self.check_database()
        redis_check = await self.check_redis()
        system_metrics = self.get_system_metrics()
        uptime = self.get_uptime()

        overall_healthy = db_check["healthy"] and redis_check["healthy"]

        return {
            "healthy": overall_healthy,
            "timestamp": datetime.now().isoformat(),
            "check_count": self.check_count,
            "uptime": uptime,
            "database": db_check,
            "redis": redis_check,
            "system": system_metrics,
        }


# 全局健康检查器实例
_health_checker: HealthChecker | None = None


def get_health_checker() -> HealthChecker:
    """获取全局健康检查器实例"""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker
