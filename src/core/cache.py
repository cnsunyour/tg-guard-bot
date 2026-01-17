"""缓存工具模块

✅ P1-10: 实施权限检查缓存，避免频繁调用 Telegram API
"""

from aiogram import Bot
from loguru import logger

from src.core.redis import get_redis
from src.core.retry import retry_on_network_error


class PermissionCache:
    """权限缓存工具"""

    # 缓存 TTL：5 分钟
    CACHE_TTL = 300

    @staticmethod
    @retry_on_network_error(max_retries=3, initial_delay=1.0)
    async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
        """检查用户是否是管理员（带缓存）

        Args:
            bot: Bot 实例
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            是否是管理员
        """
        # 构建缓存键
        cache_key = f"admin:{chat_id}:{user_id}"
        redis = get_redis()

        try:
            # 尝试从缓存获取
            cached = await redis.get(cache_key)
            if cached is not None:
                logger.debug(f"权限检查命中缓存 [群组:{chat_id}] [用户:{user_id}]")
                return cached == "1"

            # 缓存未命中，调用 Telegram API
            logger.debug(f"权限检查调用 API [群组:{chat_id}] [用户:{user_id}]")
            member = await bot.get_chat_member(chat_id, user_id)
            logger.debug(f"API 返回用户状态: {member.status} [群组:{chat_id}] [用户:{user_id}]")
            is_admin = member.status in ["creator", "administrator"]
            logger.debug(f"权限判定结果: {is_admin} [群组:{chat_id}] [用户:{user_id}]")

            # 存入缓存
            await redis.setex(cache_key, PermissionCache.CACHE_TTL, "1" if is_admin else "0")

            return is_admin

        except Exception as e:
            logger.error(f"权限检查失败 [群组:{chat_id}] [用户:{user_id}]: {e}")
            # 出错时返回 False，安全起见
            return False

    @staticmethod
    async def invalidate_admin_cache(chat_id: int, user_id: int) -> None:
        """清除特定用户的权限缓存

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID

        用于：当检测到用户权限变化时主动清除缓存
        """
        cache_key = f"admin:{chat_id}:{user_id}"
        redis = get_redis()

        try:
            await redis.delete(cache_key)
            logger.debug(f"已清除权限缓存 [群组:{chat_id}] [用户:{user_id}]")
        except Exception as e:
            logger.error(f"清除权限缓存失败: {e}")

    @staticmethod
    async def invalidate_chat_cache(chat_id: int) -> None:
        """清除整个群组的权限缓存

        Args:
            chat_id: 群组 ID

        用于：当群组管理员列表变化时批量清除
        """
        redis = get_redis()

        try:
            # 查找所有匹配的键
            pattern = f"admin:{chat_id}:*"
            cursor = 0
            deleted_count = 0

            # 使用 SCAN 避免阻塞
            while True:
                cursor, keys = await redis.scan(cursor, match=pattern, count=100)
                if keys:
                    await redis.delete(*keys)
                    deleted_count += len(keys)
                if cursor == 0:
                    break

            logger.info(f"已清除群组权限缓存 [群组:{chat_id}] [数量:{deleted_count}]")

        except Exception as e:
            logger.error(f"批量清除权限缓存失败: {e}")


class GroupConfigCache:
    """群组配置缓存

    ✅ P1-10: 缓存群组配置，减少数据库查询
    """

    # 缓存 TTL：10 分钟
    CACHE_TTL = 600

    @staticmethod
    def _get_cache_key(chat_id: int) -> str:
        """获取缓存键"""
        return f"group_config:{chat_id}"

    @staticmethod
    async def get(chat_id: int) -> dict | None:
        """从缓存获取群组配置

        Args:
            chat_id: 群组 ID

        Returns:
            群组配置字典，如果缓存未命中返回 None
        """
        redis = get_redis()
        cache_key = GroupConfigCache._get_cache_key(chat_id)

        try:
            cached = await redis.get(cache_key)
            if cached:
                import json

                logger.debug(f"群组配置命中缓存 [群组:{chat_id}]")
                return json.loads(cached)
            return None

        except Exception as e:
            logger.error(f"读取群组配置缓存失败: {e}")
            return None

    @staticmethod
    async def set(chat_id: int, config: dict) -> None:
        """设置群组配置缓存

        Args:
            chat_id: 群组 ID
            config: 群组配置字典
        """
        redis = get_redis()
        cache_key = GroupConfigCache._get_cache_key(chat_id)

        try:
            import json

            await redis.setex(
                cache_key,
                GroupConfigCache.CACHE_TTL,
                json.dumps(config, default=str),  # default=str 处理日期等类型
            )
            logger.debug(f"已设置群组配置缓存 [群组:{chat_id}]")

        except Exception as e:
            logger.error(f"设置群组配置缓存失败: {e}")

    @staticmethod
    async def invalidate(chat_id: int) -> None:
        """清除群组配置缓存

        Args:
            chat_id: 群组 ID
        """
        redis = get_redis()
        cache_key = GroupConfigCache._get_cache_key(chat_id)

        try:
            await redis.delete(cache_key)
            logger.debug(f"已清除群组配置缓存 [群组:{chat_id}]")

        except Exception as e:
            logger.error(f"清除群组配置缓存失败: {e}")
