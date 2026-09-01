"""数据定时清理服务

两类清理策略（相互独立、独立容错）：
1. spam_samples：正样本（is_spam=True）永久保留；负样本按训练比例裁剪——
   只保留最新的 正样本数 × NEGATIVE_SAMPLES_PER_POSITIVE 条。保留口径与
   SpamRepository.get_training_data() 完全一致（同一常量、同一排序），
   删除的恰好是不会参与训练的样本。
2. audit_logs：按 audit_log_retention_days 滚动删除过期记录（0 表示永久保留）。

启动间隔守卫：距上次成功运行过近（crash-loop / 滚动部署背靠背重启）时跳过
启动首轮清理，时间戳存 Redis（故障降级不阻断清理）。
"""

import asyncio
import contextlib
import time
from datetime import timedelta

from loguru import logger

from src.core.config import settings
from src.core.database import DEFAULT_DELETE_BATCH_SIZE as DELETE_BATCH_SIZE
from src.core.redis import RedisKeys, get_redis
from src.core.utils import utcnow_naive
from src.repositories.audit_repo import AuditRepository
from src.repositories.spam_repo import NEGATIVE_SAMPLES_PER_POSITIVE, SpamRepository

# 启动间隔守卫的最小间隔上限：距上次成功运行不足此值的重启跳过首轮清理。
# 取 min(interval, 3600)：24h 周期下即 1 小时——正常重启补跑一轮清理不受影响，
# 只拦截分钟级的背靠背重启
_MIN_RUN_GAP_MAX_SECONDS = 3600


class DataCleanupService:
    """数据定时清理服务（asyncio 后台循环调度）"""

    def __init__(self) -> None:
        self._interval_seconds = settings.data_cleanup_interval_hours * 3600
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """启动清理调度器"""
        if self._running:
            logger.warning("数据清理调度器已在运行")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        # 非 stop() 主动停止时（BaseException 穿透等）重置 _running，
        # 避免单例卡死「已在运行」导致清理在进程剩余生命周期内静默失效
        self._task.add_done_callback(self._on_task_done)
        logger.info("✅ 数据清理调度器已启动")

    def _on_task_done(self, task: asyncio.Task) -> None:
        """任务结束回调：异常死亡时复位状态并记录，供下次 start() 自愈"""
        if not self._running:
            return  # stop() 主动停止，正常路径
        self._running = False
        if task.cancelled():
            logger.error("数据清理调度器任务被意外取消")
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"数据清理调度器任务异常退出: {exc!r}")
        else:
            logger.error("数据清理调度器任务异常结束")

    async def stop(self) -> None:
        """停止清理调度器"""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("✅ 数据清理调度器已停止")

    async def _run_loop(self) -> None:
        """调度循环：启动即执行一轮，之后按间隔周期执行"""
        while self._running:
            # run_once 内部已逐策略捕获异常；此处再兜一层防止循环本身死亡
            # （正常情况不可达，仅防御 stats 构造等意外路径）
            try:
                await self.run_once()
            except Exception as e:
                logger.exception(f"数据清理执行失败: {e}")

            await asyncio.sleep(self._interval_seconds)

    async def run_once(self) -> dict[str, int]:
        """执行一轮清理，返回各策略删除的行数

        任一策略抛错则不记录成功时间戳（下次重启守卫放行重试）。
        """
        stats = {"spam_negative_samples": 0, "audit_logs": 0}

        if await self._recently_ran():
            logger.info("距上次数据清理成功运行过近，跳过本轮（启动间隔守卫）")
            return stats

        failed = False
        try:
            stats["spam_negative_samples"] = await self._cleanup_spam_samples()
        except Exception as e:
            failed = True
            logger.exception(f"清理 spam_samples 失败: {e}")

        try:
            stats["audit_logs"] = await self._cleanup_audit_logs()
        except Exception as e:
            failed = True
            logger.exception(f"清理 audit_logs 失败: {e}")

        if not failed:
            await self._mark_successful_run()
        return stats

    async def _recently_ran(self) -> bool:
        """距上次成功运行是否不足最小间隔

        Redis 故障或时间戳损坏时放行：守卫只是防重启风暴的辅助手段，
        宁可多跑一轮清理也不让 Redis 故障停掉数据治理。
        """
        min_gap = min(self._interval_seconds, _MIN_RUN_GAP_MAX_SECONDS)
        try:
            redis = get_redis()
            last_run = await redis.get(RedisKeys.data_cleanup_last_run())
            if last_run is not None and time.time() - float(last_run) < min_gap:
                return True
        except Exception:
            logger.warning("读取数据清理时间戳失败，跳过启动间隔守卫")
        return False

    async def _mark_successful_run(self) -> None:
        """记录本轮成功运行时间戳（写失败仅告警，不影响清理结果）"""
        try:
            redis = get_redis()
            await redis.set(
                RedisKeys.data_cleanup_last_run(),
                str(int(time.time())),
                ex=max(self._interval_seconds * 2, 7200),
            )
        except Exception as e:
            logger.warning(f"写入数据清理时间戳失败（不影响本轮清理）: {e}")

    async def _cleanup_spam_samples(self) -> int:
        """裁剪超出训练比例的负样本（正样本永久保留）"""
        # ⚠️ 已知限制（TOCTOU）：count 与删除非同一时点，窗口内正样本突增会使
        # 实际应保留的负样本多于 keep_count——多删的是最旧负样本，影响有限且
        # 窗口极小，接受此权衡（彻底消除需 count+delete 收进单条 SQL，复杂度不值）
        spam_count, normal_count = await SpamRepository.count_samples_by_label()

        if spam_count == 0:
            # 没有正样本时无法定义保留比例，且全量删除负样本过于激进，跳过本轮
            logger.info("spam_samples 无正样本，跳过负样本裁剪")
            return 0

        upper_limit = spam_count * NEGATIVE_SAMPLES_PER_POSITIVE
        if upper_limit >= normal_count:
            logger.info(
                f"spam_samples 负样本未超限（当前 {normal_count} <= 上限 {upper_limit}），无需清理"
            )
            return 0

        keep_count = min(upper_limit, normal_count)
        deleted = await SpamRepository.prune_negative_samples(keep_count, DELETE_BATCH_SIZE)
        logger.info(
            f"spam_samples 裁剪完成: 正样本={spam_count}(永久保留), "
            f"负样本保留最新 {keep_count} 条, 删除 {deleted} 条"
        )
        return deleted

    async def _cleanup_audit_logs(self) -> int:
        """按保留期删除过期的审计日志"""
        retention_days = settings.audit_log_retention_days
        if retention_days <= 0:
            logger.debug("audit_logs 保留期设为永久（0），跳过清理")
            return 0

        cutoff = utcnow_naive() - timedelta(days=retention_days)
        deleted = await AuditRepository.delete_logs_before(cutoff, DELETE_BATCH_SIZE)
        if deleted:
            logger.info(
                f"audit_logs 清理完成: 删除 {cutoff:%Y-%m-%d %H:%M} 之前的 {deleted} 条记录"
            )
        return deleted


# 全局服务实例
_service: DataCleanupService | None = None


def get_data_cleanup_service() -> DataCleanupService:
    """获取全局数据清理服务实例（无外部依赖，懒加载构造）"""
    global _service
    if _service is None:
        _service = DataCleanupService()
    return _service
