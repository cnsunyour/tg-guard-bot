"""测试 utcnow() 在实际场景中的集成测试"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, Message, User

from src.core.utils import utcnow


class TestUtcnowIntegration:
    """测试 utcnow() 在实际业务场景中的使用"""

    @pytest.mark.asyncio
    async def test_ban_chat_member_until_date(self):
        """测试 ban_chat_member 的 until_date 参数"""
        # 模拟 Bot
        bot = AsyncMock()
        bot.ban_chat_member = AsyncMock()

        # 封禁 1 小时
        chat_id = -1001234567890
        user_id = 123456789
        ban_until = utcnow() + timedelta(hours=1)

        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=ban_until,
        )

        # 验证调用参数
        bot.ban_chat_member.assert_called_once()
        call_args = bot.ban_chat_member.call_args[1]

        assert call_args["chat_id"] == chat_id
        assert call_args["user_id"] == user_id
        assert isinstance(call_args["until_date"], datetime)
        assert call_args["until_date"].tzinfo == UTC

        # 验证时间差约为 1 小时
        time_diff = (call_args["until_date"] - utcnow()).total_seconds()
        assert 3595 <= time_diff <= 3605  # 允许 5 秒误差

    @pytest.mark.asyncio
    async def test_restrict_chat_member_until_date(self):
        """测试 restrict_chat_member 的 until_date 参数"""
        bot = AsyncMock()
        bot.restrict_chat_member = AsyncMock()

        chat_id = -1001234567890
        user_id = 123456789
        # 限制 31 秒后自动解除
        until_date = utcnow() + timedelta(seconds=31)

        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=MagicMock(),
            until_date=until_date,
        )

        # 验证调用参数
        bot.restrict_chat_member.assert_called_once()
        call_args = bot.restrict_chat_member.call_args[1]

        assert call_args["until_date"].tzinfo == UTC
        time_diff = (call_args["until_date"] - utcnow()).total_seconds()
        assert 28 <= time_diff <= 33  # 允许 3 秒误差

    def test_database_timestamp_consistency(self):
        """测试数据库时间戳的一致性"""
        # 模拟数据库记录创建时间
        created_at = utcnow()

        # 模拟保存到数据库后读取
        # 数据库应该存储 UTC 时间
        db_timestamp = created_at.timestamp()

        # 从数据库恢复
        restored = datetime.fromtimestamp(db_timestamp, tz=UTC)

        # 应该完全一致（毫秒级）
        assert abs((created_at - restored).total_seconds()) < 0.001

    def test_redis_ttl_calculation(self):
        """测试 Redis TTL 计算"""
        # 验证超时时间：120 秒
        verification_timeout = 120
        expire_at = utcnow() + timedelta(seconds=verification_timeout)

        # 计算实际 TTL
        ttl = int((expire_at - utcnow()).total_seconds())

        # 应该在 118-122 秒之间（允许执行时间）
        assert 118 <= ttl <= 122

    @pytest.mark.parametrize(
        "duration_minutes,expected_hours",
        [
            (60, 1),  # 1 小时
            (1440, 24),  # 24 小时
            (10080, 168),  # 7 天
        ],
    )
    def test_moderation_ban_duration(self, duration_minutes, expected_hours):
        """测试群管功能的封禁时长计算"""
        now = utcnow()
        until_date = now + timedelta(minutes=duration_minutes)

        # 验证时间差
        diff_hours = (until_date - now).total_seconds() / 3600
        assert abs(diff_hours - expected_hours) < 0.01

        # 验证是 timezone-aware
        assert until_date.tzinfo == UTC

    def test_cross_timezone_consistency(self):
        """测试跨时区部署的一致性"""
        # 模拟不同时区的服务器获取 UTC 时间
        utc_time_1 = utcnow()

        # 即使在不同时区，UTC 时间戳应该相同
        timestamp_1 = utc_time_1.timestamp()

        # 模拟另一个时区的服务器
        utc_time_2 = datetime.now(UTC)
        timestamp_2 = utc_time_2.timestamp()

        # 时间戳差异应该小于 1 秒（执行时间）
        assert abs(timestamp_1 - timestamp_2) < 1

    def test_verification_timeout_calculation(self):
        """测试验证超时计算"""
        # 模拟验证开始
        verification_start = utcnow()
        timeout_seconds = 120

        # 计算超时时间
        timeout_at = verification_start + timedelta(seconds=timeout_seconds)

        # 验证
        assert timeout_at.tzinfo == UTC
        assert (timeout_at - verification_start).total_seconds() == timeout_seconds

        # 模拟检查是否超时
        now = utcnow()
        is_timeout = now >= timeout_at
        assert isinstance(is_timeout, bool)

    @pytest.mark.asyncio
    async def test_spam_detection_timestamp(self):
        """测试反垃圾消息的时间戳记录"""
        # 模拟消息
        message = MagicMock(spec=Message)
        message.date = utcnow()
        message.text = "测试消息"
        message.from_user = MagicMock(spec=User)
        message.from_user.id = 123456789
        message.chat = MagicMock(spec=Chat)
        message.chat.id = -1001234567890

        # 模拟检测时间
        detected_at = utcnow()

        # 验证时间一致性
        assert message.date.tzinfo == UTC
        assert detected_at.tzinfo == UTC

        # 检测时间应该晚于或等于消息时间
        assert detected_at >= message.date

    def test_audit_log_timestamp(self):
        """测试审计日志时间戳"""
        # 模拟审计日志记录
        log_entry = {
            "action": "ban_user",
            "operator_id": 987654321,
            "target_id": 123456789,
            "timestamp": utcnow(),
            "reason": "spam",
        }

        # 验证时间戳
        assert log_entry["timestamp"].tzinfo == UTC

        # 验证可以序列化为 ISO 格式
        iso_str = log_entry["timestamp"].isoformat()
        assert "+" in iso_str or "Z" in iso_str  # 包含时区信息

        # 可以反序列化
        restored = datetime.fromisoformat(iso_str)
        assert restored.tzinfo is not None

    def test_naive_vs_aware_comparison(self):
        """测试 naive 和 aware datetime 的对比"""
        aware_now = utcnow()
        naive_now = datetime.utcnow()

        # 旧代码的问题：naive datetime 在某些时区会有 8 小时偏差
        # 这个测试验证新代码的正确性

        # aware datetime 的 timestamp 是正确的 UTC 时间戳
        aware_ts = aware_now.timestamp()

        # naive datetime 的 timestamp 会被解释为本地时间
        # 在 +0800 时区，会比实际 UTC 时间少 8 小时（28800 秒）
        naive_ts = naive_now.timestamp()

        # 在 UTC 时区，两者应该相等
        # 在其他时区，aware 版本才是正确的
        if aware_ts != naive_ts:
            # 说明在非 UTC 时区，验证修复有效
            print(f"时区偏差检测: aware_ts={aware_ts}, naive_ts={naive_ts}")
            print(f"差异: {aware_ts - naive_ts} 秒")

        # 无论在什么时区，aware 版本都应该返回正确的 UTC 时间戳
        assert aware_now.tzinfo == UTC
