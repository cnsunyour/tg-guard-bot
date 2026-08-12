"""测试 utcnow() 时区修复的正确性"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.utils import utcnow


class TestUtcnow:
    """测试 utcnow() 函数"""

    def test_utcnow_returns_aware_datetime(self):
        """utcnow() 应该返回 timezone-aware datetime"""
        now = utcnow()
        assert now.tzinfo is not None
        assert now.tzinfo == UTC

    def test_utcnow_vs_datetime_now_utc(self):
        """utcnow() 应该等价于 datetime.now(timezone.utc)"""
        now1 = utcnow()
        now2 = datetime.now(UTC)
        # 允许 1 秒误差
        assert abs((now1 - now2).total_seconds()) < 1

    def test_utcnow_timestamp_consistency(self):
        """utcnow() 的 timestamp 应该与 UTC 一致"""
        now = utcnow()
        timestamp = now.timestamp()

        # 从 timestamp 恢复应该得到相同的时间
        restored = datetime.fromtimestamp(timestamp, tz=UTC)
        assert abs((now - restored).total_seconds()) < 0.001

    def test_utcnow_arithmetic(self):
        """utcnow() 支持时间算术"""
        now = utcnow()
        future = now + timedelta(hours=1)

        assert future.tzinfo == UTC
        assert (future - now).total_seconds() == 3600

    def test_utcnow_vs_naive_utcnow(self):
        """对比 utcnow() 和旧的 datetime.utcnow()"""
        aware_now = utcnow()
        naive_now = datetime.utcnow()

        # naive datetime 的 timestamp 会受本地时区影响
        aware_ts = aware_now.timestamp()
        naive_ts = naive_now.replace(tzinfo=UTC).timestamp()

        # 应该在 1 秒内相同
        assert abs(aware_ts - naive_ts) < 1

    def test_telegram_until_date_scenario(self):
        """测试 Telegram until_date 场景"""
        # 模拟封禁 1 小时
        ban_until = utcnow() + timedelta(hours=1)

        # 验证是 aware datetime
        assert ban_until.tzinfo == UTC

        # 验证 timestamp 正确（应该是未来 3600 秒）
        now_ts = utcnow().timestamp()
        ban_ts = ban_until.timestamp()
        diff = ban_ts - now_ts

        # 允许 2 秒误差（执行时间）
        assert 3598 <= diff <= 3602

    def test_multiple_calls_monotonic(self):
        """多次调用应该返回单调递增的时间"""
        times = [utcnow() for _ in range(10)]

        for i in range(len(times) - 1):
            assert times[i] <= times[i + 1]

    @pytest.mark.parametrize("hours", [1, 24, 168])
    def test_future_dates(self, hours):
        """测试未来时间计算"""
        now = utcnow()
        future = now + timedelta(hours=hours)

        assert future > now
        assert future.tzinfo == UTC
        assert (future - now).total_seconds() == hours * 3600
