"""Redis 连接管理模块"""

from redis.asyncio import ConnectionPool, Redis

from src.core.config import settings

# 全局 Redis 连接池和客户端
_redis_pool: ConnectionPool | None = None
_redis_client: Redis | None = None


def get_redis_pool() -> ConnectionPool:
    """获取 Redis 连接池"""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = ConnectionPool.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


def get_redis() -> Redis:
    """获取 Redis 客户端"""
    global _redis_client
    if _redis_client is None:
        pool = get_redis_pool()
        _redis_client = Redis(connection_pool=pool)
    return _redis_client


async def close_redis() -> None:
    """关闭 Redis 连接"""
    global _redis_client, _redis_pool
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
    if _redis_pool:
        await _redis_pool.disconnect()
        _redis_pool = None


class RedisKeys:
    """Redis 键名常量"""

    @staticmethod
    def verification(chat_id: int, user_id: int) -> str:
        """验证码键名"""
        return f"verification:{chat_id}:{user_id}"

    @staticmethod
    def rate_limit(user_id: int, chat_id: int) -> str:
        """频率限制键名"""
        return f"rate_limit:{chat_id}:{user_id}"

    @staticmethod
    def group_config(chat_id: int) -> str:
        """群组配置缓存键名"""
        return f"group_config:{chat_id}"

    @staticmethod
    def user_warnings(chat_id: int, user_id: int) -> str:
        """用户警告计数键名"""
        return f"warnings:{chat_id}:{user_id}"

    @staticmethod
    def spam_message_text(chat_id: int, message_id: int) -> str:
        """垃圾消息文本缓存键名

        ✅ P1-12: 缓存垃圾消息原始文本，用于管理员反馈
        """
        return f"spam_text:{chat_id}:{message_id}"

    @staticmethod
    def verification_hint(chat_id: int) -> str:
        """验证引导消息记录键名

        用于防止多用户同时未启动 Bot 时重复发送引导消息
        """
        return f"verification_hint:{chat_id}"

    @staticmethod
    def verification_approved(chat_id: int, user_id: int) -> str:
        """验证已批准标记键名

        用于标记已通过验证的用户，避免批准加入后重复验证
        """
        return f"verification_approved:{chat_id}:{user_id}"

    @staticmethod
    def verification_type(chat_id: int, user_id: int) -> str:
        """验证类型标记键名

        用于标记验证是加入请求验证(join_request)还是正常验证(normal)
        """
        return f"verification_type:{chat_id}:{user_id}"
