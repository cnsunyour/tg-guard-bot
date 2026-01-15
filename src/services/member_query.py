"""成员查询服务（基于 Telethon）"""

import json
from datetime import datetime

from loguru import logger
from telethon import TelegramClient
from telethon.tl.types import (
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis


class MemberQueryService:
    """基于 Telethon 的成员查询服务（带 Redis 缓存）"""

    def __init__(self, client: TelegramClient):
        self.client = client

    async def get_members(self, chat_id: int, force_refresh: bool = False) -> list[dict]:
        """获取群组成员列表（优先使用缓存）

        Args:
            chat_id: 群组 ID
            force_refresh: 强制刷新缓存

        Returns:
            成员列表 [{"user_id": int, "deleted": bool, "status": str}, ...]
        """
        redis = get_redis()
        cache_key = RedisKeys.cleanup_members_cache(chat_id)

        # 尝试从缓存获取
        if not force_refresh:
            cached = await redis.get(cache_key)
            if cached:
                try:
                    data = json.loads(cached)
                    logger.debug(f"从缓存获取群组 {chat_id} 成员列表: {len(data['members'])} 人")
                    return data["members"]
                except Exception as e:
                    logger.warning(f"解析缓存失败: {e}")

        # 从 Telethon 获取
        logger.info(f"从 Telethon 获取群组 {chat_id} 成员列表...")
        try:
            participants = await self.client.get_participants(chat_id)
            members = [
                {
                    "user_id": p.id,
                    "deleted": p.deleted,
                    "status": self._get_status_name(p.status),
                }
                for p in participants
            ]

            logger.info(f"获取到 {len(members)} 个成员")

            # 写入缓存
            cache_data = {
                "chat_id": chat_id,
                "cached_at": datetime.utcnow().isoformat(),
                "members": members,
            }
            await redis.setex(cache_key, settings.cleanup_cache_ttl, json.dumps(cache_data))

            return members

        except Exception as e:
            logger.error(f"获取群组成员失败: {e}")
            raise

    async def get_deleted_users(self, chat_id: int) -> list[int]:
        """获取已删除用户 ID 列表

        Args:
            chat_id: 群组 ID

        Returns:
            已删除用户 ID 列表
        """
        members = await self.get_members(chat_id)
        deleted = [m["user_id"] for m in members if m["deleted"]]
        logger.info(f"群组 {chat_id} 有 {len(deleted)} 个已删除用户")
        return deleted

    async def get_inactive_users(self, chat_id: int, status: str) -> list[int]:
        """获取指定不活跃状态的用户

        Args:
            chat_id: 群组 ID
            status: 不活跃状态 ("long_time_ago" | "last_month" | "last_week")

        Returns:
            不活跃用户 ID 列表
        """
        members = await self.get_members(chat_id)
        inactive = [m["user_id"] for m in members if self._should_cleanup(m["status"], status)]
        logger.info(f"群组 {chat_id} 有 {len(inactive)} 个 {status} 状态的用户")
        return inactive

    async def refresh_cache(self, chat_id: int) -> int:
        """强制刷新缓存，返回成员数量

        Args:
            chat_id: 群组 ID

        Returns:
            成员数量
        """
        members = await self.get_members(chat_id, force_refresh=True)
        return len(members)

    async def get_cache_info(self, chat_id: int) -> dict | None:
        """获取缓存信息

        Args:
            chat_id: 群组 ID

        Returns:
            缓存信息 {"cached_at": str, "member_count": int, "ttl_seconds": int}
            如果缓存不存在则返回 None
        """
        redis = get_redis()
        cache_key = RedisKeys.cleanup_members_cache(chat_id)
        cached = await redis.get(cache_key)
        if cached:
            try:
                data = json.loads(cached)
                ttl = await redis.ttl(cache_key)
                return {
                    "cached_at": data["cached_at"],
                    "member_count": len(data["members"]),
                    "ttl_seconds": ttl,
                }
            except Exception as e:
                logger.warning(f"解析缓存信息失败: {e}")
        return None

    def _get_status_name(self, status) -> str:
        """解析用户状态

        Args:
            status: Telethon UserStatus 对象

        Returns:
            状态名称字符串
        """
        if isinstance(status, UserStatusOnline):
            return "online"
        elif isinstance(status, UserStatusOffline):
            return "offline"
        elif isinstance(status, UserStatusRecently):
            return "recently"
        elif isinstance(status, UserStatusLastWeek):
            return "last_week"
        elif isinstance(status, UserStatusLastMonth):
            return "last_month"
        elif isinstance(status, UserStatusEmpty):
            return "long_time_ago"
        return "unknown"

    def _should_cleanup(self, user_status: str, target: str) -> bool:
        """判断是否应该清理

        Args:
            user_status: 用户状态
            target: 目标状态阈值

        Returns:
            是否应该清理
        """
        # 状态严重程度排序
        severity = {
            "online": 0,
            "offline": 1,
            "recently": 2,
            "last_week": 3,
            "last_month": 4,
            "long_time_ago": 5,
        }
        target_severity = severity.get(target, 5)
        user_severity = severity.get(user_status, -1)
        return user_severity >= target_severity
