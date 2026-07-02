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
