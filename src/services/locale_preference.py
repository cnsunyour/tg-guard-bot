"""语言偏好写入服务

将 DB 权威写与 Redis 写穿组合成一个应用层操作，避免写穿顺序散落在 handler。

返回值语义（POC）：返回值表示 DB 权威写是否成功。Redis 写失败只记录日志、
不回滚 DB——DB 是偏好权威来源，缓存只负责加速读取，将由 TTL 或下一次写入
收敛。多进程并发切换的分布式锁在 POC 阶段未引入，采用 last-write-wins。
"""

from loguru import logger

from src.core.i18n.resolver import LocaleResolver
from src.repositories.group_repo import GroupRepository
from src.repositories.user_settings_repo import UserSettingsRepository


class LocalePreferenceService:
    """群组与用户语言偏好写入服务"""

    def __init__(self, resolver: LocaleResolver) -> None:
        # 复用 middleware 持有的 resolver（含 cache/supported_locales），不在服务内重构造
        self.resolver = resolver

    def _is_supported(self, locale: str) -> bool:
        return locale in self.resolver.supported_locales

    async def set_group_locale(self, chat_id: int, locale: str) -> bool:
        """更新群组语言

        Returns:
            DB 是否成功提交。缓存写失败不回滚 DB（依赖 TTL 收敛）。
        """
        if not self._is_supported(locale):
            logger.warning(f"拒绝写入不支持的群组 locale [群组:{chat_id}] [locale:{locale}]")
            return False

        try:
            updated = await GroupRepository.update_locale(chat_id, locale)
        except Exception as exc:
            logger.error(f"更新群组 locale 失败 [群组:{chat_id}] [locale:{locale}]: {exc}")
            return False

        if not updated:
            logger.warning(f"群组不存在，未更新 locale [群组:{chat_id}]")
            return False

        try:
            cache_written = await self.resolver.cache.set_group(chat_id, locale, nx=False)
        except Exception as exc:
            cache_written = False
            logger.error(f"写入群组 locale 缓存异常 [群组:{chat_id}]: {exc}")

        if not cache_written:
            logger.warning(
                f"群组 locale 已写入 DB，但缓存写入失败，将依赖 TTL 收敛 "
                f"[群组:{chat_id}] [locale:{locale}]"
            )
        return True

    async def set_user_locale(self, user_id: int, locale: str) -> bool:
        """更新用户私聊语言

        Returns:
            DB upsert 是否成功。缓存写失败不影响 DB 成功语义。
        """
        if not self._is_supported(locale):
            logger.warning(f"拒绝写入不支持的用户 locale [用户:{user_id}] [locale:{locale}]")
            return False

        try:
            await UserSettingsRepository.upsert_locale(user_id, locale)
        except Exception as exc:
            logger.error(f"更新用户 locale 失败 [用户:{user_id}] [locale:{locale}]: {exc}")
            return False

        try:
            cache_written = await self.resolver.cache.set_user(user_id, locale, nx=False)
        except Exception as exc:
            cache_written = False
            logger.error(f"写入用户 locale 缓存异常 [用户:{user_id}]: {exc}")

        if not cache_written:
            logger.warning(
                f"用户 locale 已写入 DB，但缓存写入失败，将依赖 TTL 收敛 "
                f"[用户:{user_id}] [locale:{locale}]"
            )
        return True
