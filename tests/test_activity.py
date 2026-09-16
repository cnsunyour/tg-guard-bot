"""测试活跃度服务：衰减下限与非文本消息拦截"""

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.services.activity import ActivityService


def _mock_redis(get_side_effect):
    """构造 fake redis，其 get 按序列返回给定值"""
    redis = AsyncMock()
    redis.get.side_effect = get_side_effect
    return redis


@pytest.mark.asyncio
async def test_new_user_without_activity_key_returns_zero():
    """从未发言（无 activity key）→ 活跃度 0"""
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis([None])
        assert await ActivityService.get_activity(1, 100) == 0


@pytest.mark.asyncio
async def test_new_user_blocked_from_non_text_when_enabled():
    """从未发言用户在已启用群被拦截非文本消息"""
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis([None])
        allowed, current = await ActivityService.check_non_text_allowed(1, 100, True)
        assert (allowed, current) == (False, 0)


@pytest.mark.asyncio
async def test_posted_user_decays_to_floor_not_zero():
    """曾发言用户长期衰减后不低于 floor（默认 1），不会被误判为 0"""
    old_date = (date.today() - timedelta(days=20)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis(["5", old_date])
        assert await ActivityService.get_activity(1, 100) == 1


@pytest.mark.asyncio
async def test_decayed_old_user_allowed_non_text():
    """衰减到 floor 的老用户放行非文本消息"""
    old_date = (date.today() - timedelta(days=20)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis(["5", old_date])
        allowed, current = await ActivityService.check_non_text_allowed(1, 100, True)
        assert (allowed, current) == (True, 1)


@pytest.mark.asyncio
async def test_floor_zero_restores_old_behavior(monkeypatch):
    """activity_decay_floor=0 时退回旧行为（可衰减到 0）"""
    monkeypatch.setattr("src.services.activity.settings.activity_decay_floor", 0)
    old_date = (date.today() - timedelta(days=20)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis(["5", old_date])
        assert await ActivityService.get_activity(1, 100) == 0


@pytest.mark.asyncio
async def test_disabled_group_allows_non_text():
    """activity 关闭的群不限制非文本（早返回）"""
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis([None])
        allowed, current = await ActivityService.check_non_text_allowed(1, 100, False)
        assert (allowed, current) == (True, 0)


@pytest.mark.asyncio
async def test_positive_activity_allows_non_text():
    """活跃度 > 0 时放行非文本消息"""
    today = date.today().isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis(["3", today])
        allowed, current = await ActivityService.check_non_text_allowed(1, 100, True)
        assert (allowed, current) == (True, 3)


@pytest.mark.asyncio
async def test_record_non_text_skips_new_user():
    """从未发言用户记录非文本消息不应创建 activity key（保持新人状态）"""
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        mock_redis.get.return_value = None  # 无 activity key
        result = await ActivityService.record_non_text_message(1, 100)
        assert result == 0
        mock_redis.set.assert_not_called()  # 不创建 key


@pytest.mark.asyncio
async def test_record_non_text_updates_posted_user():
    """曾发言用户记录非文本消息正常更新（不误删 key）"""
    today = date.today().isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        # get 顺序：record 的存在性检查、get_activity 的 activity、get_activity 的 last_date
        mock_redis.get.side_effect = ["3", "3", today]
        result = await ActivityService.record_non_text_message(1, 100)
        assert result == 3  # NON_TEXT_PENALTY=0，不变
        mock_redis.set.assert_called()  # 正常写回


@pytest.mark.asyncio
async def test_stale_zero_activity_key_not_lifted_by_floor():
    """存量 key=0（历史脏数据）不被 floor 反向抬升，保持 0"""
    old_date = (date.today() - timedelta(days=10)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis(["0", old_date])
        assert await ActivityService.get_activity(1, 100) == 0


@pytest.mark.asyncio
async def test_activity_at_floor_not_decayed():
    """活跃度已等于 floor 时不再衰减（保持原值，不被 max 抬升也不下探）"""
    old_date = (date.today() - timedelta(days=10)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis(["1", old_date])
        assert await ActivityService.get_activity(1, 100) == 1


@pytest.mark.asyncio
async def test_activity_above_floor_decays_to_floor():
    """活跃度高于 floor 时正常衰减，结果不低于 floor"""
    old_date = (date.today() - timedelta(days=3)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_get.return_value = _mock_redis(["2", old_date])
        # 2 - 3 天 = -1，max(-1, floor=1) = 1
        assert await ActivityService.get_activity(1, 100) == 1


# ===== 超短消息不增加活跃度（与垃圾检测共用 SPAM_MIN_TEXT_LENGTH 阈值）=====

# 标准化长度低于默认阈值 10 的消息（占位探活/极短互动典型形态）
SHORT_TEXTS = [".", "。", "...", "？", "1", "666", "ok", "你好", "👍", "   "]

# 标准化长度达到默认阈值 10 的消息（13 汉字 / 31 半角字符）
LONG_TEXTS = ["这是一条足够长的测试消息哈", "hello world this is a test message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", SHORT_TEXTS)
async def test_record_text_message_skips_short(text):
    """超短消息不加活跃度、不写任何 key（保持新人 0 状态）"""
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        mock_redis.get.return_value = None  # 新用户无 activity key
        assert await ActivityService.record_text_message(1, 100, text=text) == 0
        mock_redis.set.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", SHORT_TEXTS)
async def test_record_text_message_short_keeps_current(text):
    """已发言用户发超短消息：返回当前值不加分"""
    today = date.today().isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        # get 顺序：get_activity 的 activity、last_date
        mock_redis.get.side_effect = ["3", today]
        assert await ActivityService.record_text_message(1, 100, text=text) == 3
        mock_redis.set.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", LONG_TEXTS)
async def test_record_text_message_counts_long(text):
    """达到最小文本长度的消息照常 +1 并刷新日期"""
    today = date.today().isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        mock_redis.get.side_effect = ["3", today]
        assert await ActivityService.record_text_message(1, 100, text=text) == 4
        assert mock_redis.set.call_count == 2  # activity + last_date


@pytest.mark.asyncio
async def test_record_text_message_threshold_boundary(monkeypatch):
    """阈值边界：标准化长度恰等于阈值计分、低于阈值不计（含 0=禁用语义）"""
    today = date.today().isoformat()
    monkeypatch.setattr("src.services.activity.settings.spam_min_text_length", 2)
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        # get 顺序：超短分支 get_activity 第 1 个 get 即 None 早退（仅耗 1 个）；
        # 有效分支 get_activity 耗 2 个（activity + last_date）
        mock_redis.get.side_effect = [None, "3", today]
        assert await ActivityService.record_text_message(1, 100, text="好") == 0  # 长度 1 < 2
        assert await ActivityService.record_text_message(1, 100, text="你好") == 4  # 长度 2 >= 2
        assert mock_redis.set.call_count == 2

    # 阈值 0 = 禁用过滤（与检测侧「设为 0 禁用」语义一致），任何文本都计
    monkeypatch.setattr("src.services.activity.settings.spam_min_text_length", 0)
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        mock_redis.get.side_effect = ["3", today]
        assert await ActivityService.record_text_message(1, 100, text=".") == 4


@pytest.mark.asyncio
async def test_record_text_message_short_does_not_reset_decay_clock():
    """超短消息不重置衰减时钟：懒惰衰减结果返回但不写回 Redis"""
    old_date = (date.today() - timedelta(days=20)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        mock_redis.get.side_effect = ["5", old_date]
        # 衰减到 floor=1 的值返回给调用方，但 set 不写回（下次读取重复计算同样结果，幂等）
        assert await ActivityService.record_text_message(1, 100, text=".") == 1
        mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_consecutive_short_reads_are_idempotent():
    """连续多条超短消息：均按原值衰减计算（未到下限），不累计多扣"""
    old_date = (date.today() - timedelta(days=2)).isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        # stored=5、2 天未发言 → 每次读取都从原值算：5-2=3（不写回故不累计）
        mock_redis.get.side_effect = ["5", old_date] * 3
        for _ in range(3):
            assert await ActivityService.record_text_message(1, 100, text=".") == 3
        mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_record_text_message_none_keeps_legacy_behavior():
    """text=None 保持旧行为（无条件 +1，兼容未知文本的调用方）"""
    today = date.today().isoformat()
    with patch("src.services.activity.get_redis") as mock_get:
        mock_redis = AsyncMock()
        mock_get.return_value = mock_redis
        mock_redis.get.side_effect = ["3", today]
        assert await ActivityService.record_text_message(1, 100) == 4
        assert mock_redis.set.call_count == 2
