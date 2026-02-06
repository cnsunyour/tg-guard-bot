"""username → user_id 映射服务"""

from aiogram import Bot
from loguru import logger

from src.core.redis import RedisKeys, get_redis


class UsernameMappingService:
    """username → user_id 全局映射服务"""

    @staticmethod
    async def update_mapping(user_id: int, username: str) -> None:
        """更新 username 映射

        Args:
            user_id: 用户 ID
            username: 用户名（不带 @ 符号）
        """
        if not username:
            # 用户没有 username，跳过
            return

        redis = get_redis()
        username_lower = username.lower()

        # 创建/更新映射
        mapping_key = RedisKeys.username_mapping(username_lower)
        await redis.setex(mapping_key, 604800, str(user_id))  # 7 天 TTL

        logger.debug(f"已更新 username 映射: @{username_lower} -> {user_id}")

    @staticmethod
    async def get_user_id_by_username(username: str, bot: Bot, chat_id: int) -> int | None:
        """通过 username 获取 user_id（带 API 实时验证）

        流程：
        1. 从映射表获取缓存的 user_id
        2. 通过 get_chat_member API 查询用户信息
        3. 验证 username 是否匹配
        4. 不匹配则删除缓存并返回 None

        Args:
            username: 用户名（可带或不带 @ 符号）
            bot: Bot 实例（用于 API 调用）
            chat_id: 群组 ID（用于 API 调用）

        Returns:
            user_id 或 None
        """
        redis = get_redis()
        username_lower = username.lower().lstrip("@")

        # 1. 从缓存获取映射
        mapping_key = RedisKeys.username_mapping(username_lower)
        cached_user_id_str = await redis.get(mapping_key)

        if not cached_user_id_str:
            logger.debug(f"未找到 username 映射缓存: @{username_lower}")
            return None

        cached_user_id = int(cached_user_id_str)

        # 2. 通过 API 实时验证
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=cached_user_id)

            # 3. 验证 username 是否匹配
            if member.user.username and member.user.username.lower() == username_lower:
                logger.debug(
                    f"username 映射验证成功: @{username_lower} -> {cached_user_id} "
                    f"(实时验证通过)"
                )
                return cached_user_id
            else:
                # username 不匹配，说明用户已更改 username，删除缓存
                await redis.delete(mapping_key)
                logger.info(
                    f"username 映射已过期: @{username_lower} <-> {cached_user_id} "
                    f"(当前 username: @{member.user.username or 'None'})，已删除缓存"
                )
                return None

        except Exception as e:
            # API 调用失败（用户可能离开群组或删除账号）
            logger.warning(f"API 验证失败 [user_id:{cached_user_id}]: {e}")
            # 保留缓存，等待下次更新或 TTL 过期
            return None
