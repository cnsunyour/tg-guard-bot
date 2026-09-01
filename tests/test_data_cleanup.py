"""数据定时清理服务测试"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.core.config import settings
from src.repositories.spam_repo import NEGATIVE_SAMPLES_PER_POSITIVE, SpamRepository
from src.services import data_cleanup
from src.services.data_cleanup import DataCleanupService


@pytest.fixture
def cleanup_service(mocker):
    service = DataCleanupService()
    # 默认放行启动间隔守卫（Redis 调用不出测试网）；需要守卫语义的用例显式覆盖
    mocker.patch.object(service, "_recently_ran", new=AsyncMock(return_value=False))
    mocker.patch.object(service, "_mark_successful_run", new=AsyncMock())
    return service


def _mock_label_counts(mocker, spam_count: int, normal_count: int):
    return mocker.patch.object(
        data_cleanup.SpamRepository,
        "count_samples_by_label",
        new=AsyncMock(return_value=(spam_count, normal_count)),
    )


class TestSpamSamplesCleanup:
    """负样本按训练比例裁剪，正样本永久保留"""

    async def test_prunes_excess_negatives_with_training_ratio(self, cleanup_service, mocker):
        """负样本超出 正样本×训练比例 时，保留最新 keep 条并删除其余"""
        _mock_label_counts(mocker, spam_count=10, normal_count=500)
        prune = mocker.patch.object(
            data_cleanup.SpamRepository,
            "prune_negative_samples",
            new=AsyncMock(return_value=300),
        )

        deleted = await cleanup_service._cleanup_spam_samples()

        assert deleted == 300
        prune.assert_awaited_once_with(
            10 * NEGATIVE_SAMPLES_PER_POSITIVE, data_cleanup.DELETE_BATCH_SIZE
        )

    async def test_skips_when_no_positives(self, cleanup_service, mocker):
        """无正样本时跳过：比例无意义且全量删除负样本过于激进"""
        _mock_label_counts(mocker, spam_count=0, normal_count=100)
        prune = mocker.patch.object(
            data_cleanup.SpamRepository, "prune_negative_samples", new=AsyncMock()
        )

        assert await cleanup_service._cleanup_spam_samples() == 0
        prune.assert_not_awaited()

    async def test_noop_when_within_ratio(self, cleanup_service, mocker):
        """负样本未超上限时不触发删除"""
        # 上限 = 10 × 20 = 200 >= 150
        _mock_label_counts(mocker, spam_count=10, normal_count=150)
        prune = mocker.patch.object(
            data_cleanup.SpamRepository, "prune_negative_samples", new=AsyncMock()
        )

        assert await cleanup_service._cleanup_spam_samples() == 0
        prune.assert_not_awaited()


class TestPruneNegativeSamplesGuard:
    """prune_negative_samples 仓库层守卫"""

    async def test_rejects_non_positive_keep_count(self):
        """keep_count<=0 会删除全部负样本，仓库层直接拒绝"""
        for bad_count in (0, -1):
            with pytest.raises(ValueError, match="keep_count"):
                await SpamRepository.prune_negative_samples(bad_count)


class TestAuditLogsCleanup:
    """审计日志按配置保留期滚动删除"""

    async def test_deletes_before_cutoff(self, cleanup_service, mocker):
        """删除 cutoff = now - retention_days 之前的记录"""
        mocker.patch.object(settings, "audit_log_retention_days", 30)
        fixed_now = datetime(2026, 9, 1, 12, 0, 0)
        mocker.patch.object(data_cleanup, "utcnow_naive", return_value=fixed_now)
        delete = mocker.patch.object(
            data_cleanup.AuditRepository, "delete_logs_before", new=AsyncMock(return_value=42)
        )

        deleted = await cleanup_service._cleanup_audit_logs()

        assert deleted == 42
        delete.assert_awaited_once_with(
            fixed_now - timedelta(days=30), data_cleanup.DELETE_BATCH_SIZE
        )

    async def test_skips_when_retention_zero(self, cleanup_service, mocker):
        """保留期设为 0（永久保留）时跳过"""
        mocker.patch.object(settings, "audit_log_retention_days", 0)
        delete = mocker.patch.object(
            data_cleanup.AuditRepository, "delete_logs_before", new=AsyncMock()
        )

        assert await cleanup_service._cleanup_audit_logs() == 0
        delete.assert_not_awaited()


class TestRunOnce:
    async def test_isolates_strategy_failures(self, cleanup_service, mocker):
        """spam_samples 清理抛错不影响 audit_logs 清理"""
        mocker.patch.object(
            data_cleanup.SpamRepository,
            "count_samples_by_label",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        )
        mocker.patch.object(settings, "audit_log_retention_days", 365)
        mocker.patch.object(data_cleanup, "utcnow_naive", return_value=datetime(2026, 9, 1))
        audit_delete = mocker.patch.object(
            data_cleanup.AuditRepository, "delete_logs_before", new=AsyncMock(return_value=7)
        )

        stats = await cleanup_service.run_once()

        assert stats == {"spam_negative_samples": 0, "audit_logs": 7}
        audit_delete.assert_awaited_once()

    async def test_skips_run_when_recently_ran(self, mocker):
        """启动间隔守卫：距上次成功运行过近时整轮跳过"""
        service = DataCleanupService()
        mocker.patch.object(service, "_recently_ran", new=AsyncMock(return_value=True))
        spam_cleanup = mocker.patch.object(
            service, "_cleanup_spam_samples", new=AsyncMock(return_value=99)
        )
        audit_cleanup = mocker.patch.object(
            service, "_cleanup_audit_logs", new=AsyncMock(return_value=99)
        )

        stats = await service.run_once()

        assert stats == {"spam_negative_samples": 0, "audit_logs": 0}
        spam_cleanup.assert_not_awaited()
        audit_cleanup.assert_not_awaited()

    async def test_no_success_mark_when_strategy_failed(self, mocker):
        """任一策略抛错时不写成功时间戳（下次重启守卫放行重试）"""
        service = DataCleanupService()
        mocker.patch.object(service, "_recently_ran", new=AsyncMock(return_value=False))
        mark = mocker.patch.object(service, "_mark_successful_run", new=AsyncMock())
        mocker.patch.object(
            service, "_cleanup_spam_samples", new=AsyncMock(side_effect=RuntimeError("db down"))
        )
        mocker.patch.object(service, "_cleanup_audit_logs", new=AsyncMock(return_value=3))

        await service.run_once()

        mark.assert_not_awaited()


class TestStartupGapGuard:
    """启动间隔守卫的 Redis 交互与降级"""

    async def test_recently_ran_true_within_gap(self, mocker):
        """时间戳距现在不足最小间隔 → 拦截"""
        service = DataCleanupService()
        redis = mocker.patch.object(data_cleanup, "get_redis").return_value
        redis.get = AsyncMock(return_value=str(0))  # 距今远超间隔的上古时间戳

        assert await service._recently_ran() is False

        redis.get = AsyncMock(return_value="99999999999999")  # 未来时间戳，必然 < gap
        assert await service._recently_ran() is True

    async def test_recently_ran_degrades_on_redis_failure(self, mocker):
        """Redis 故障时守卫放行，不阻断清理"""
        service = DataCleanupService()
        mocker.patch.object(
            data_cleanup, "get_redis", side_effect=RuntimeError("redis unavailable")
        )

        assert await service._recently_ran() is False


class TestSchedulerLifecycle:
    async def test_start_stop_idempotent(self, cleanup_service, mocker):
        """start/stop 可重复调用；运行中的循环不触达真实数据库"""
        mocker.patch.object(
            cleanup_service,
            "run_once",
            new=AsyncMock(return_value={"spam_negative_samples": 0, "audit_logs": 0}),
        )

        await cleanup_service.start()
        assert cleanup_service._running

        await cleanup_service.start()  # 重复启动仅告警，不新建任务
        assert cleanup_service._running

        await cleanup_service.stop()
        assert not cleanup_service._running

        await cleanup_service.stop()  # 重复停止为 no-op
        assert not cleanup_service._running

    async def test_resets_running_when_task_dies(self, cleanup_service, mocker):
        """任务异常死亡（非 stop 主动停止）时复位 _running，允许再次 start 自愈"""
        mocker.patch.object(
            cleanup_service,
            "run_once",
            new=AsyncMock(side_effect=BaseException("killed")),
        )

        await cleanup_service.start()
        assert cleanup_service._running

        # 让事件循环跑完注定死亡的任务，done_callback 应复位 _running
        with pytest.raises(BaseException, match="killed"):
            await cleanup_service._task

        assert not cleanup_service._running
