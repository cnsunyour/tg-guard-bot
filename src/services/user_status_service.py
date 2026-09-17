"""用户状态检测服务（基于 Telethon）"""

import asyncio
import contextlib
import json
from dataclasses import dataclass

from loguru import logger
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import ChannelParticipant, ChannelParticipantBanned

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis
from src.core.utils import utcnow


@dataclass
class UserStatusResult:
    """用户状态检查结果"""

    is_problematic: bool
    user_id: int
    reason: str | None = None  # restricted/scam/fake/deleted
    error: str | None = None
    cached: bool = False


class UserStatusService:
    """用户状态检测服务（基于 Telethon）

    检测用户是否为异常状态：
    - restricted: 被 Telegram 限制的用户
    - scam: 被标记为诈骗的用户
    - fake: 被标记为虚假账号的用户
    - deleted: 已删除的账号
    """

    def __init__(self, client: TelegramClient | None = None) -> None:
        self._client = client

    @property
    def enabled(self) -> bool:
        """检查服务是否可用"""
        return settings.user_status_check_enabled and self._client is not None

    async def get_user_bio(self, chat_id: int, user_id: int) -> str | None:
        """获取用户 bio（Telethon full user 的 about 字段）

        降级策略与 check_user 一致：未启用或任何失败（含 userbot 不在群组、
        session 无该用户实体）返回 None，由调用方决定是否走 Bot API getChat 兜底。

        Args:
            chat_id: 群组 ID（经群组上下文解析用户实体，userbot 须在该群）
            user_id: 用户 ID

        Returns:
            用户 bio；无 bio 或获取失败时为 None
        """
        client = self._client
        if not self.enabled or client is None:
            return None

        try:
            user = await self._get_participant_entity(chat_id, user_id)
            if user is None:
                return None

            result = await client(GetFullUserRequest(id=user))
            return result.full_user.about or None
        except Exception as e:
            logger.debug(f"Telethon 获取用户 bio 失败 [用户:{user_id}]: {e}")
            return None

    async def check_user(self, user_id: int, chat_id: int | None = None) -> UserStatusResult:
        """检查用户状态

        降级策略：
        - Telethon 未启用：跳过检查（is_problematic=False）
        - API/网络/解析失败：放行（is_problematic=False），避免误伤正常用户

        Args:
            user_id: 用户 ID
            chat_id: 群组 ID（可选，提供则检查该群组的成员状态）

        Returns:
            UserStatusResult 检查结果
        """
        # 未启用 Telethon 或功能未启用
        if not self.enabled:
            return UserStatusResult(is_problematic=False, user_id=user_id)

        redis = get_redis()
        cache_key = RedisKeys.user_status_result(user_id)

        # 1) 缓存
        try:
            cached = await redis.get(cache_key)
        except Exception as e:
            logger.debug(f"用户状态缓存读取失败 [用户:{user_id}]: {e}")
            cached = None

        if cached is not None:
            try:
                data = json.loads(cached)
                result = UserStatusResult(
                    is_problematic=data["is_problematic"],
                    user_id=user_id,
                    reason=data.get("reason"),
                    cached=True,
                )
                return result
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"用户状态缓存解析失败 [用户:{user_id}]: {e}")

        # 2) 分布式锁：防止并发请求
        lock_key = RedisKeys.user_status_lock(user_id)
        lock_error = False
        try:
            lock_acquired = await redis.set(lock_key, "1", nx=True, ex=10)
        except Exception as e:
            logger.debug(f"用户状态锁获取失败 [用户:{user_id}]: {e}")
            lock_acquired = False
            lock_error = True

        if not lock_acquired and not lock_error:
            # 有其他协程在查，稍等后读缓存
            await asyncio.sleep(0.5)
            with contextlib.suppress(Exception):
                cached = await redis.get(cache_key)
                if cached is not None:
                    data = json.loads(cached)
                    result = UserStatusResult(
                        is_problematic=data["is_problematic"],
                        user_id=user_id,
                        reason=data.get("reason"),
                        cached=True,
                    )
                    return result

            return UserStatusResult(is_problematic=False, user_id=user_id, error="concurrent")

        if lock_error:
            logger.debug(f"用户状态 Redis 不可用，直连 Telethon [用户:{user_id}]")

        try:
            # 3) 调用 Telethon 检查用户状态
            result = await self._check_user_status(user_id, chat_id)

            # 4) 写缓存
            try:
                cache_data = {
                    "is_problematic": result.is_problematic,
                    "reason": result.reason,
                    "checked_at": utcnow().isoformat(),
                }
                await redis.setex(cache_key, settings.user_status_cache_ttl, json.dumps(cache_data))
            except Exception as e:
                logger.debug(f"用户状态缓存写入失败 [用户:{user_id}]: {e}")

            return result

        except Exception as e:
            # 降级放行
            logger.exception(f"用户状态检查异常 [用户:{user_id}]: {e}")
            return UserStatusResult(is_problematic=False, user_id=user_id, error=str(e))

        finally:
            # 确保锁被释放
            if lock_acquired:
                with contextlib.suppress(Exception):
                    await redis.delete(lock_key)

    async def _check_user_status(
        self, user_id: int, chat_id: int | None = None
    ) -> UserStatusResult:
        """实际执行用户状态检查（带重试）"""
        if not self._client:
            return UserStatusResult(
                is_problematic=False, user_id=user_id, error="client_not_available"
            )

        max_retries = settings.user_status_max_retries
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                # 如果提供了 chat_id，优先检查该群组的成员状态（更准确）
                if chat_id:
                    user = await self._get_participant_entity(chat_id, user_id)
                else:
                    # 直接获取用户实体
                    user = await self._client.get_entity(user_id)

                # 检查用户状态
                if getattr(user, "deleted", False):
                    return UserStatusResult(is_problematic=True, user_id=user_id, reason="deleted")

                if getattr(user, "restricted", False):
                    return UserStatusResult(
                        is_problematic=True, user_id=user_id, reason="restricted"
                    )

                if getattr(user, "scam", False):
                    return UserStatusResult(is_problematic=True, user_id=user_id, reason="scam")

                if getattr(user, "fake", False):
                    return UserStatusResult(is_problematic=True, user_id=user_id, reason="fake")

                # 正常用户
                return UserStatusResult(is_problematic=False, user_id=user_id)

            except FloodWaitError as e:
                last_error = e
                if attempt < max_retries:
                    wait_time = min(e.seconds, 10)  # 最多等待 10 秒
                    logger.warning(
                        f"用户状态检查触发速率限制 [用户:{user_id}] "
                        f"[attempt={attempt + 1}/{max_retries + 1}] "
                        f"[wait={wait_time}s]"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"用户状态检查失败，已达最大重试次数 [用户:{user_id}]，降级放行")
                    return UserStatusResult(is_problematic=False, user_id=user_id, error=str(e))

            except (UserNotParticipantError, ChannelPrivateError, ValueError) as e:
                # 用户不在群组或群组不可访问，降级放行
                logger.debug(f"用户状态检查失败 [用户:{user_id}]: {e}")
                return UserStatusResult(is_problematic=False, user_id=user_id, error=str(e))

            except Exception as e:
                # 其他异常不重试，直接降级放行
                logger.debug(f"用户状态检查异常 [用户:{user_id}]: {e}")
                return UserStatusResult(is_problematic=False, user_id=user_id, error=str(e))

        # 理论上不会到达这里（所有重试都失败）
        if last_error:
            return UserStatusResult(is_problematic=False, user_id=user_id, error=str(last_error))
        return UserStatusResult(is_problematic=False, user_id=user_id, error="unknown")

    async def _get_participant_entity(self, chat_id: int, user_id: int):
        """获取群组成员实体（更准确的状态信息）

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            User 实体

        Raises:
            UserNotParticipantError: 用户不在群组中
            ChannelPrivateError: 群组不可访问
        """
        if not self._client:
            raise ValueError("Telethon client not available")

        try:
            result = await self._client(GetParticipantRequest(channel=chat_id, participant=user_id))

            # 检查是否被封禁
            if isinstance(result.participant, ChannelParticipantBanned):
                # 被封禁的用户，返回用户实体（包含状态信息）
                return result.users[0] if result.users else None

            # 正常参与者，返回用户实体
            if isinstance(result.participant, ChannelParticipant):
                return result.users[0] if result.users else None

            # 其他情况，返回用户实体
            return result.users[0] if result.users else None
        except Exception:
            # 如果 GetParticipantRequest 失败，直接获取用户实体
            return await self._client.get_entity(user_id)


# 全局实例（由 main.py 初始化）
_user_status_service: UserStatusService | None = None


def init_user_status_service(client: TelegramClient | None = None) -> None:
    """初始化用户状态服务

    Args:
        client: Telethon 客户端实例（可选）
    """
    global _user_status_service
    _user_status_service = UserStatusService(client)


def get_user_status_service() -> UserStatusService:
    """获取用户状态服务单例"""
    global _user_status_service
    if _user_status_service is None:
        _user_status_service = UserStatusService()
    return _user_status_service
