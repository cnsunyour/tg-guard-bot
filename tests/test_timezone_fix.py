"""时区修复测试套件"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.utils import utcnow


class TestUTCNowFunction:
    """测试 utcnow() 函数"""

    def test_utcnow_returns_aware_datetime(self):
        """utcnow() 应返回带时区信息的 datetime"""
        now = utcnow()
        assert now.tzinfo is not None, "utcnow() 应返回 timezone-aware datetime"
        assert now.tzinfo == UTC, "时区应为 UTC"

    def test_utcnow_vs_deprecated(self):
        """utcnow() 应等价于 datetime.now(timezone.utc)"""
        standard_utc = datetime.now(UTC)
        our_utc = utcnow()

        # 允许 1 秒误差（调用间隔）
        diff = abs((standard_utc - our_utc).total_seconds())
        assert diff < 1.0, f"时间差应小于 1 秒，实际: {diff}"

    def test_utcnow_timestamp_compatibility(self):
        """utcnow() 应能正确转换为 timestamp"""
        now = utcnow()
        ts = now.timestamp()

        # 验证 timestamp 可以还原
        restored = datetime.fromtimestamp(ts, tz=UTC)
        assert abs((restored - now).total_seconds()) < 0.01

    def test_utcnow_arithmetic(self):
        """utcnow() 应支持日期时间运算"""
        now = utcnow()
        future = now + timedelta(hours=1)
        past = now - timedelta(days=1)

        assert future > now
        assert past < now
        assert (future - now).total_seconds() == 3600
        assert (now - past).total_seconds() == 86400


class TestTimezoneConsistency:
    """测试时区一致性"""

    def test_no_naive_datetime_leak(self):
        """确保没有 naive datetime 泄露"""
        now = utcnow()

        # 验证可以与其他 aware datetime 进行比较
        other_aware = datetime.now(UTC)
        assert now <= other_aware or now >= other_aware  # 不应抛出 TypeError

    def test_timezone_aware_comparison(self):
        """测试 timezone-aware datetime 的比较"""
        utc_time = utcnow()

        # 不同时区的相同时刻应该相等
        from datetime import timezone as tz

        offset_8 = tz(timedelta(hours=8))
        beijing_time = utc_time.astimezone(offset_8)

        # 虽然显示不同，但表示同一时刻
        assert utc_time.timestamp() == beijing_time.timestamp()


class TestVerificationTimestamp:
    """测试验证相关的时间戳场景"""

    def test_verification_timeout_calculation(self):
        """测试验证超时计算"""
        now = utcnow()
        timeout_seconds = 120
        timeout_time = now + timedelta(seconds=timeout_seconds)

        # 验证时间间隔
        diff = (timeout_time - now).total_seconds()
        assert abs(diff - timeout_seconds) < 0.01

    def test_until_date_31_seconds_trick(self):
        """测试 Telegram restrict_chat_member 的 31 秒技巧"""
        now = utcnow()
        until_date = now + timedelta(seconds=31)

        # 验证 until_date 在未来
        assert until_date > now

        # 验证间隔正确
        diff = (until_date - now).total_seconds()
        assert 30 < diff <= 32  # 允许微小误差


class TestIntegrationScenarios:
    """集成场景测试"""

    def test_cross_timezone_timestamp_conversion(self):
        """测试跨时区 timestamp 转换"""
        # 模拟 Telegram API 返回的 timestamp（总是 UTC）
        utc_now = utcnow()
        telegram_ts = int(utc_now.timestamp())

        # 转换回 datetime（带时区）
        restored = datetime.fromtimestamp(telegram_ts, tz=UTC)

        # 应该在 1 秒内（因为取整）
        diff = abs((restored - utc_now).total_seconds())
        assert diff < 1.0

    def test_old_vs_new_utcnow_timestamp_difference(self):
        """测试新旧 utcnow() 在非 UTC 时区的差异"""
        import os

        # 保存原始 TZ
        original_tz = os.environ.get("TZ")

        try:
            # 模拟在 +0800 时区运行
            os.environ["TZ"] = "Asia/Shanghai"

            # 新实现（正确）
            aware_now = utcnow()
            aware_ts = aware_now.timestamp()

            # 旧实现（错误 - 仅用于对比）
            naive_now = datetime.utcnow()  # 返回 naive UTC 时间
            naive_ts = naive_now.timestamp()  # ❌ 会被当作本地时间！

            # 在 +0800 时区，差异应该是 8 小时（28800 秒）
            diff = abs(aware_ts - naive_ts)
            print(f"\n时区偏差检测: aware_ts={aware_ts}, naive_ts={naive_ts}")
            print(f"差异: {diff} 秒")

            # 在 +0800 时区，差异应该接近 8 小时
            # 注意：这个测试揭示了旧代码的 bug
            assert diff > 1.0, "应该检测到时区差异"

        finally:
            # 恢复原始 TZ
            if original_tz is not None:
                os.environ["TZ"] = original_tz
            elif "TZ" in os.environ:
                del os.environ["TZ"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
