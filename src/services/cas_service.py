"""CAS (Combot Anti-Spam) API 服务"""

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis


@dataclass
class CASCheckResult:
    """CAS 检查结果"""

    is_banned: bool
    user_id: int
    offenses: int = 0
    time_added: datetime | None = None
    error: str | None = None
    cached: bool = False


class CASService:
    """CAS (Combot Anti-Spam) API 服务"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._base_url = settings.cas_api_url.rstrip("/")

    @property
    def client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端（延迟初始化）"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.cas_check_timeout, connect=3.0),
                limits=httpx.Limits(max_keepalive_connections=2, max_connections=5),
            )
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def check_user(self, user_id: int) -> CASCheckResult:
        """检查用户是否在 CAS 黑名单中

        降级策略：
        - API/网络/解析失败：放行（is_banned=False），避免误伤正常用户
        """

        redis = get_redis()
        cache_key = RedisKeys.cas_result(user_id)

        # 1) 缓存
        try:
            cached = await redis.get(cache_key)
        except Exception as e:
            logger.debug(f"CAS 缓存读取失败 [用户:{user_id}]: {e}")
            cached = None

        if cached is not None:
            try:
                data = json.loads(cached)
                result = self._parse_response(user_id, data)
                result.cached = True
                return result
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"CAS 缓存解析失败 [用户:{user_id}]: {e}")

        # 2) 分布式锁：防止并发请求
        lock_key = RedisKeys.cas_lock(user_id)
        lock_error = False
        try:
            lock_acquired = await redis.set(lock_key, "1", nx=True, ex=10)
        except Exception as e:
            logger.debug(f"CAS 锁获取失败 [用户:{user_id}]: {e}")
            lock_acquired = False
            lock_error = True

        if not lock_acquired and not lock_error:
            # 有其他协程在查，稍等后读缓存
            await asyncio.sleep(0.5)
            try:
                cached = await redis.get(cache_key)
                if cached is not None:
                    data = json.loads(cached)
                    result = self._parse_response(user_id, data)
                    result.cached = True
                    return result
            except Exception:
                pass

            return CASCheckResult(is_banned=False, user_id=user_id, error="concurrent")

        if lock_error:
            logger.debug(f"CAS Redis 不可用，直连 API [用户:{user_id}]")

        try:
            # 3) 调用 CAS API
            response = await self.client.get(
                f"{self._base_url}/check",
                params={"user_id": user_id},
            )
            response.raise_for_status()
            data = response.json()

            result = self._parse_response(user_id, data)

            # 4) 写缓存
            try:
                await redis.setex(cache_key, settings.cas_cache_ttl, json.dumps(data))
            except Exception as e:
                logger.debug(f"CAS 缓存写入失败 [用户:{user_id}]: {e}")

            return result

        except httpx.HTTPStatusError as e:
            logger.error(
                f"CAS API HTTP 错误 [用户:{user_id}] [状态:{e.response.status_code}]"
            )
            return CASCheckResult(is_banned=False, user_id=user_id, error=str(e))
        except httpx.RequestError as e:
            logger.error(f"CAS API 请求失败 [用户:{user_id}]: {e}")
            return CASCheckResult(is_banned=False, user_id=user_id, error=str(e))
        except Exception as e:
            logger.exception(f"CAS 检查异常 [用户:{user_id}]: {e}")
            return CASCheckResult(is_banned=False, user_id=user_id, error=str(e))
        finally:
            if lock_acquired:
                with contextlib.suppress(Exception):
                    await redis.delete(lock_key)

    @staticmethod
    def _parse_response(user_id: int, data: dict[str, Any]) -> CASCheckResult:
        """解析 CAS API 响应

        CAS API 响应格式：
        - 黑名单: {"ok": true, "result": {"offenses": 3, "time_added": 1234567890}}
        - 正常:   {"ok": false, "description": "Record not found."}
        """

        if not data.get("ok", False):
            return CASCheckResult(is_banned=False, user_id=user_id)

        result = data.get("result", {})
        offenses = int(result.get("offenses", 0) or 0)

        time_added = None
        if ts := result.get("time_added"):
            with contextlib.suppress(Exception):
                time_added = datetime.fromtimestamp(int(ts), tz=UTC)

        return CASCheckResult(
            is_banned=True,
            user_id=user_id,
            offenses=offenses,
            time_added=time_added,
        )


_cas_service: CASService | None = None


def get_cas_service() -> CASService:
    """获取 CAS 服务单例"""

    global _cas_service
    if _cas_service is None:
        _cas_service = CASService()
    return _cas_service
