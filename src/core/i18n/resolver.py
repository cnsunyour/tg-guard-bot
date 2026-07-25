"""群组与用户语言偏好解析

提供按「消息目的地」解析 locale 的能力。ContextVar 只能给出当前 Update
的便利默认值；当发送目标是别的聊天（如群流程触发的私聊）、或来自定时/
延迟任务时，必须用这里的显式解析。
"""

from loguru import logger

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis
from src.repositories.group_repo import GroupRepository
from src.repositories.user_settings_repo import UserSettingsRepository

# 用户偏好缓存哨兵：空串表示「已查询但数据库无记录」，用于防穿透并
# 让 for_user_explicit 能区分「无记录」与「显式选择默认语言」。
_NO_RECORD_SENTINEL = ""


class LocalePreferenceCache:
    """语言偏好 Redis 缓存

    Redis 异常时返回未命中，交由 LocaleResolver 继续查询数据库。
    """

    def __init__(self, ttl_seconds: int | None = None) -> None:
        self.ttl_seconds = ttl_seconds or settings.locale_cache_ttl_seconds

    async def get_group(self, chat_id: int) -> str | None:
        """读取群组语言缓存"""
        return await self._get(RedisKeys.locale_group(chat_id), "群组", chat_id)

    async def set_group(self, chat_id: int, locale: str, *, nx: bool = False) -> bool:
        """写入群组语言缓存，返回 Redis 写入结果（权威写 nx=False；回填 nx=True）"""
        return await self._set(RedisKeys.locale_group(chat_id), locale, "群组", chat_id, nx=nx)

    async def invalidate_group(self, chat_id: int) -> None:
        """清除群组语言缓存"""
        await self._invalidate(RedisKeys.locale_group(chat_id), "群组", chat_id)

    async def get_user(self, user_id: int) -> str | None:
        """读取用户语言缓存"""
        return await self._get(RedisKeys.locale_user(user_id), "用户", user_id)

    async def set_user(self, user_id: int, locale: str, *, nx: bool = False) -> bool:
        """写入用户语言缓存，返回 Redis 写入结果（权威写 nx=False；回填 nx=True）"""
        return await self._set(RedisKeys.locale_user(user_id), locale, "用户", user_id, nx=nx)

    async def invalidate_user(self, user_id: int) -> None:
        """清除用户语言缓存"""
        await self._invalidate(RedisKeys.locale_user(user_id), "用户", user_id)

    async def _get(self, key: str, subject_type: str, subject_id: int) -> str | None:
        try:
            return await get_redis().get(key)
        except Exception as exc:
            logger.error(f"读取{subject_type}语言缓存失败 [ID:{subject_id}] [键:{key}]: {exc}")
            return None

    async def _set(
        self,
        key: str,
        locale: str,
        subject_type: str,
        subject_id: int,
        nx: bool = False,
    ) -> bool:
        """写入缓存并返回 Redis 写入结果；nx=True 时仅当键不存在才写入"""
        try:
            if nx:
                result = await get_redis().set(key, locale, nx=True, ex=self.ttl_seconds)
            else:
                result = await get_redis().setex(key, self.ttl_seconds, locale)
            return bool(result)
        except Exception as exc:
            logger.error(f"写入{subject_type}语言缓存失败 [ID:{subject_id}] [键:{key}]: {exc}")
            return False

    async def _invalidate(self, key: str, subject_type: str, subject_id: int) -> None:
        try:
            await get_redis().delete(key)
        except Exception as exc:
            logger.error(f"清除{subject_type}语言缓存失败 [ID:{subject_id}] [键:{key}]: {exc}")


class LocaleResolver:
    """异步 locale 解析器"""

    def __init__(self, cache: LocalePreferenceCache | None = None) -> None:
        self.cache = cache or LocalePreferenceCache()
        self.default_locale = settings.default_locale
        self.supported_locales = set(settings.supported_locales)

    async def for_group(self, chat_id: int) -> str:
        """解析群组消息语言：Redis → Group.locale → 默认语言"""
        cached = await self.cache.get_group(chat_id)
        if cached is not None:
            if cached in self.supported_locales:
                return cached
            logger.warning(f"群组语言缓存非法，重新查询 [群组:{chat_id}] [locale:{cached}]")
            await self.cache.invalidate_group(chat_id)

        try:
            group = await GroupRepository.get(chat_id)
            locale = group.locale if group else None
        except Exception as exc:
            logger.error(f"查询群组语言失败 [群组:{chat_id}]: {exc}")
            return self.default_locale

        resolved = self._normalize(locale, f"群组:{chat_id}")
        # 群组无记录也缓存解析结果，避免持续穿透数据库。
        # 用 NX 回填：若期间 /lang 已权威写入新值，则不覆盖。
        await self.cache.set_group(chat_id, resolved, nx=True)
        return resolved

    async def for_user(self, user_id: int) -> str:
        """解析用户私聊语言：显式偏好 → 默认语言；查询失败 → 默认语言"""
        locale, ok = await self._resolve_user_explicit(user_id)
        if not ok:
            return self.default_locale
        return self._normalize(locale, f"用户:{user_id}")

    async def for_user_explicit(self, user_id: int) -> str | None:
        """返回用户显式语言偏好，None 表示确认无记录

        ⚠️ 查询失败（Redis/DB 异常）或数据库含非法 locale 时返回默认语言，
        与「显式选择默认语言」不可区分。**不要**用本方法判断用户是否设置过
        偏好（/lang 的选中态应直接查 DB）；需要区分查询失败的场景请使用底层
        _resolve_user_explicit 的三态返回。

        返回值已做合法性归一。
        """
        locale, ok = await self._resolve_user_explicit(user_id)
        if not ok:
            return self.default_locale
        if locale is None:
            return None
        return locale if locale in self.supported_locales else self.default_locale

    async def for_private_from_group(
        self,
        user_id: int,
        group_chat_id: int,
    ) -> str:
        """解析群流程触发的私聊语言

        产品规则（选项 B）：
        1. 用户存在显式偏好（含显式选择默认语言）→ 使用用户偏好；
        2. 用户查询失败（状态未知）→ 默认语言，不误用来源群语言；
        3. 用户确认无记录 → 使用来源群语言；
        4. 群语言不可用 → 由 for_group 降级到默认语言。

        关键点：依靠 _resolve_user_explicit 的三态区分「显式偏好」「确认无记录」
        与「查询失败」，避免显式默认语言被来源群覆盖，也避免查询失败时误用群语言。
        """
        locale, ok = await self._resolve_user_explicit(user_id)
        if not ok:
            return self.default_locale
        if locale is not None:
            return self._normalize(locale, f"用户:{user_id}")
        return await self.for_group(group_chat_id)

    async def _resolve_user_explicit(self, user_id: int) -> tuple[str | None, bool]:
        """返回 (显式 locale 或 None, 查询是否成功)

        - (locale, True): 用户有显式偏好 locale；
        - (None, True): 已确认数据库无记录；
        - (None, False): 查询失败，状态未知（不缓存）。

        三态区分让调用方能在查询失败时回退默认语言，而非误用群语言。
        """
        cached = await self.cache.get_user(user_id)
        if cached is not None:
            if cached == _NO_RECORD_SENTINEL:
                return None, True
            if cached in self.supported_locales:
                return cached, True
            # 非法缓存值，清除后回源（与群缓存一致）
            logger.warning(f"用户语言缓存非法，重新查询 [用户:{user_id}] [locale:{cached}]")
            await self.cache.invalidate_user(user_id)

        try:
            locale = await UserSettingsRepository.get_locale(user_id)
        except Exception as exc:
            logger.error(f"查询用户语言失败 [用户:{user_id}]: {exc}")
            return None, False

        # None = 确认无记录，缓存哨兵防穿透
        if locale is None:
            await self.cache.set_user(user_id, _NO_RECORD_SENTINEL, nx=True)
            return None, True

        # 非法值（空串或不支持的 locale）视为数据异常：按查询失败处理，不缓存，
        # 避免把脏数据固化进缓存后反复降级。
        if locale not in self.supported_locales:
            logger.error(f"数据库包含不支持的用户 locale [用户:{user_id}] [locale:{locale}]")
            return None, False

        # 合法显式偏好，用 NX 回填（不覆盖并发 /lang 的权威写入）
        await self.cache.set_user(user_id, locale, nx=True)
        return locale, True

    def _normalize(self, locale: str | None, subject: str) -> str:
        if locale is None:
            return self.default_locale
        if locale not in self.supported_locales:
            logger.error(
                f"数据库包含不支持的 locale [{subject}] "
                f"[locale:{locale}]，降级到 {self.default_locale}"
            )
            return self.default_locale
        return locale
