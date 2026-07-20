"""CAS 服务测试"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.services.cas_service import CASService


@pytest.fixture
def cas_service():
    """创建 CAS 服务实例"""
    return CASService()


@pytest.fixture
def mock_redis():
    """模拟 Redis 客户端"""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    return redis


@pytest.mark.asyncio
async def test_check_user_success(cas_service, mock_redis):
    """测试成功检查用户"""
    user_id = 123456789

    # Mock HTTP 响应 - 用户不在黑名单
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"ok": False, "description": "Record not found."}
    mock_response.raise_for_status = MagicMock()

    with patch("src.services.cas_service.get_redis", return_value=mock_redis):
        cas_service._client = AsyncMock()
        cas_service._client.get = AsyncMock(return_value=mock_response)

        result = await cas_service.check_user(user_id)

        assert result.is_banned is False
        assert result.user_id == user_id
        assert result.error is None
        assert result.cached is False


@pytest.mark.asyncio
async def test_check_user_banned(cas_service, mock_redis):
    """测试检查到黑名单用户"""
    user_id = 123456789

    # Mock HTTP 响应 - 用户在黑名单
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "ok": True,
        "result": {
            "offenses": 3,
            "time_added": 1234567890,
        },
    }
    mock_response.raise_for_status = MagicMock()

    with patch("src.services.cas_service.get_redis", return_value=mock_redis):
        cas_service._client = AsyncMock()
        cas_service._client.get = AsyncMock(return_value=mock_response)

        result = await cas_service.check_user(user_id)

        assert result.is_banned is True
        assert result.user_id == user_id
        assert result.offenses == 3
        assert result.error is None


@pytest.mark.asyncio
async def test_check_user_retry_on_network_error(cas_service, mock_redis):
    """测试网络错误时的指数退避重试"""
    user_id = 123456789

    # 第一次请求失败，第二次成功
    mock_error = httpx.RequestError("Connection error")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"ok": False}
    mock_response.raise_for_status = MagicMock()

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise mock_error
        return mock_response

    with patch("src.services.cas_service.get_redis", return_value=mock_redis):
        cas_service._client = AsyncMock()
        cas_service._client.get = AsyncMock(side_effect=side_effect)

        result = await cas_service.check_user(user_id)

        # 应该重试一次后成功
        assert call_count == 2
        assert result.is_banned is False
        assert result.error is None


@pytest.mark.asyncio
async def test_check_user_retry_max_attempts(cas_service, mock_redis):
    """测试达到最大重试次数后降级放行"""
    user_id = 123456789

    # 所有请求都失败
    mock_error = httpx.RequestError("Connection error")

    with patch("src.services.cas_service.get_redis", return_value=mock_redis):
        cas_service._client = AsyncMock()
        cas_service._client.get = AsyncMock(side_effect=mock_error)

        result = await cas_service.check_user(user_id)

        # 应该尝试 3 次（初始请求 + 2 次重试）
        assert cas_service._client.get.call_count == 3
        assert result.is_banned is False
        assert result.error is not None
        assert "Connection error" in result.error


@pytest.mark.asyncio
async def test_check_user_timeout_includes_phase_and_effective_timeout(cas_service, mock_redis):
    """空消息超时仍记录阶段、有效超时值并按配置重试"""
    user_id = 123456789

    mock_client = AsyncMock()
    mock_client.timeout = httpx.Timeout(5, connect=3.0)
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))

    with (
        patch("src.services.cas_service.get_redis", return_value=mock_redis),
        patch("src.services.cas_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        cas_service._client = mock_client
        result = await cas_service.check_user(user_id)

    # 应尝试 3 次（初始 + 2 次重试），重试间两次 sleep
    assert mock_client.get.await_count == 3
    assert mock_sleep.await_count == 2
    assert result.is_banned is False
    assert result.error == "[error_type=ReadTimeout] [phase=read] [timeout_seconds=5]"


@pytest.mark.asyncio
async def test_check_user_from_cache(cas_service, mock_redis):
    """测试从缓存读取结果"""
    user_id = 123456789
    cached_data = json.dumps({"ok": False, "description": "Record not found."})

    mock_redis.get = AsyncMock(return_value=cached_data)

    with patch("src.services.cas_service.get_redis", return_value=mock_redis):
        result = await cas_service.check_user(user_id)

        assert result.is_banned is False
        assert result.cached is True
        # 不应该调用 HTTP 客户端
        assert cas_service._client is None or not cas_service._client.get.called


@pytest.mark.asyncio
async def test_parse_response_normal(cas_service):
    """测试解析正常用户响应"""
    data = {"ok": False, "description": "Record not found."}
    result = cas_service._parse_response(123456789, data)

    assert result.is_banned is False
    assert result.user_id == 123456789
    assert result.offenses == 0


@pytest.mark.asyncio
async def test_parse_response_banned(cas_service):
    """测试解析黑名单用户响应"""
    data = {
        "ok": True,
        "result": {
            "offenses": 5,
            "time_added": 1234567890,
        },
    }
    result = cas_service._parse_response(123456789, data)

    assert result.is_banned is True
    assert result.user_id == 123456789
    assert result.offenses == 5
    assert result.time_added is not None
