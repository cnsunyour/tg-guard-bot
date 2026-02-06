"""测试 username → user_id 映射功能"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.username_mapping import UsernameMappingService


@pytest.mark.asyncio
async def test_update_mapping():
    """测试基本的映射更新"""
    with patch("src.services.username_mapping.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # 更新映射
        await UsernameMappingService.update_mapping(123456, "testuser")

        # 验证 Redis 调用
        mock_redis.setex.assert_called_once_with("username_map:testuser", 604800, "123456")


@pytest.mark.asyncio
async def test_update_mapping_no_username():
    """测试 username 为空的情况"""
    with patch("src.services.username_mapping.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # username 为 None，不应调用 Redis
        await UsernameMappingService.update_mapping(123456, None)

        # 验证没有调用 Redis
        mock_redis.setex.assert_not_called()


@pytest.mark.asyncio
async def test_get_user_id_by_username_success():
    """测试成功查询 username 映射"""
    with patch("src.services.username_mapping.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # Mock Redis 返回缓存的 user_id
        mock_redis.get.return_value = "123456"

        # Mock Bot API 调用
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock()
        member_mock = MagicMock()
        member_mock.user.username = "testuser"
        bot_mock.get_chat_member.return_value = member_mock

        # 查询映射
        user_id = await UsernameMappingService.get_user_id_by_username(
            "testuser", bot=bot_mock, chat_id=123
        )

        # 验证结果
        assert user_id == 123456
        mock_redis.get.assert_called_once_with("username_map:testuser")
        bot_mock.get_chat_member.assert_called_once_with(chat_id=123, user_id=123456)


@pytest.mark.asyncio
async def test_get_user_id_by_username_not_cached():
    """测试缓存未命中的情况"""
    with patch("src.services.username_mapping.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # Mock Redis 返回 None（未缓存）
        mock_redis.get.return_value = None

        bot_mock = MagicMock()

        # 查询映射
        user_id = await UsernameMappingService.get_user_id_by_username(
            "testuser", bot=bot_mock, chat_id=123
        )

        # 验证返回 None
        assert user_id is None
        mock_redis.get.assert_called_once_with("username_map:testuser")
        # 不应调用 API
        bot_mock.get_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_get_user_id_by_username_username_changed():
    """测试 username 变更的场景"""
    with patch("src.services.username_mapping.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # Mock Redis 返回缓存的 user_id
        mock_redis.get.return_value = "123456"

        # Mock Bot API 返回不同的 username（用户已更改）
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock()
        member_mock = MagicMock()
        member_mock.user.username = "newuser"
        bot_mock.get_chat_member.return_value = member_mock

        # 查询映射
        user_id = await UsernameMappingService.get_user_id_by_username(
            "olduser", bot=bot_mock, chat_id=123
        )

        # 验证返回 None 并删除缓存
        assert user_id is None
        mock_redis.delete.assert_called_once_with("username_map:olduser")


@pytest.mark.asyncio
async def test_get_user_id_by_username_api_error():
    """测试 API 调用失败的情况"""
    with patch("src.services.username_mapping.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # Mock Redis 返回缓存的 user_id
        mock_redis.get.return_value = "123456"

        # Mock Bot API 抛出异常
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock(side_effect=Exception("API Error"))

        # 查询映射
        user_id = await UsernameMappingService.get_user_id_by_username(
            "testuser", bot=bot_mock, chat_id=123
        )

        # 验证返回 None（保留缓存）
        assert user_id is None
        # 不应删除缓存
        mock_redis.delete.assert_not_called()


@pytest.mark.asyncio
async def test_case_insensitive():
    """测试大小写不敏感"""
    with patch("src.services.username_mapping.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis

        # Mock Redis 返回缓存的 user_id
        mock_redis.get.return_value = "123456"

        # Mock Bot API 调用
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock()
        member_mock = MagicMock()
        member_mock.user.username = "TestUser"
        bot_mock.get_chat_member.return_value = member_mock

        # 查询各种大小写组合
        user_id1 = await UsernameMappingService.get_user_id_by_username(
            "testuser", bot=bot_mock, chat_id=123
        )
        user_id2 = await UsernameMappingService.get_user_id_by_username(
            "TESTUSER", bot=bot_mock, chat_id=123
        )
        user_id3 = await UsernameMappingService.get_user_id_by_username(
            "TestUser", bot=bot_mock, chat_id=123
        )

        # 验证所有查询都成功
        assert user_id1 == 123456
        assert user_id2 == 123456
        assert user_id3 == 123456

        # 验证所有查询都转为小写
        assert mock_redis.get.call_count == 3
        mock_redis.get.assert_any_call("username_map:testuser")
