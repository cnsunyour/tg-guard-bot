"""宵禁模式调度器"""

import asyncio
import contextlib

from aiogram import Bot
from loguru import logger
from sqlalchemy import select

from src.core.database import get_db_session
from src.core.i18n import LocaleResolver, Translator
from src.models.group import Group
from src.services.curfew import CurfewService


class CurfewScheduler:
    """宵禁模式调度器

    定期检查所有启用宵禁的群组，发送进入/退出通知
    """

    def __init__(
        self,
        bot: Bot,
        locale_resolver: LocaleResolver,
        translator: Translator,
    ) -> None:
        self.bot = bot
        # 定时任务无 Update 上下文，ContextVar 不可用，须按群显式解析 locale。
        self.locale_resolver = locale_resolver
        self.translator = translator
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            logger.warning("宵禁调度器已在运行")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("✅ 宵禁调度器已启动")

    async def stop(self) -> None:
        """停止调度器"""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("✅ 宵禁调度器已停止")

    async def _run_loop(self) -> None:
        """调度循环"""
        while self._running:
            try:
                await self._check_all_groups()
            except Exception as e:
                logger.error(f"宵禁检查失败: {e}")

            # 每分钟检查一次
            await asyncio.sleep(60)

    async def _check_all_groups(self) -> None:
        """检查所有群组的宵禁状态"""
        async with get_db_session() as session:
            result = await session.execute(select(Group).where(Group.curfew_enabled))
            groups = result.scalars().all()

        for group in groups:
            try:
                await self._check_group(group)
            except Exception as e:
                logger.error(f"检查群组 {group.id} 宵禁状态失败: {e}")

    async def _check_group(self, group: Group) -> None:
        """检查单个群组的宵禁状态"""
        # 每次按群解析 locale（同一轮可能遍历不同语言的群，禁止缓存 localizer 到实例）
        locale = await self.locale_resolver.for_group(group.id)
        localizer = self.translator.for_locale(locale)

        is_in_curfew = CurfewService.is_in_curfew(group)
        entered, exited = await CurfewService.track_curfew_state(group.id, is_in_curfew)

        if entered:
            start_time = f"{group.curfew_start_hour:02d}:{group.curfew_start_minute:02d}"
            end_time = f"{group.curfew_end_hour:02d}:{group.curfew_end_minute:02d}"
            timezone = f"{group.curfew_timezone_offset:+d}"
            # 发送进入宵禁通知
            try:
                await self.bot.send_message(
                    chat_id=group.id,
                    text=localizer.t(
                        "curfew.scheduler.entered.group.message",
                        start_time=start_time,
                        end_time=end_time,
                        timezone=timezone,
                    ),
                    parse_mode="HTML",
                )
                logger.info(f"已发送宵禁进入通知 [群组:{group.id}]")
            except Exception as e:
                logger.error(f"发送宵禁进入通知失败 [群组:{group.id}]: {e}")

        elif exited:
            # 发送退出宵禁通知
            try:
                await self.bot.send_message(
                    chat_id=group.id,
                    text=localizer.t("curfew.scheduler.exited.group.message"),
                    parse_mode="HTML",
                )
                logger.info(f"已发送宵禁退出通知 [群组:{group.id}]")
            except Exception as e:
                logger.error(f"发送宵禁退出通知失败 [群组:{group.id}]: {e}")


# 全局调度器实例
_scheduler: CurfewScheduler | None = None


def get_curfew_scheduler(
    bot: Bot | None = None,
    locale_resolver: LocaleResolver | None = None,
    translator: Translator | None = None,
) -> CurfewScheduler:
    """获取全局宵禁调度器实例。

    首次构造须传入 bot/locale_resolver/translator；后续取已构造单例（如 shutdown
    仅 stop）可不传参。
    """
    global _scheduler
    if _scheduler is None:
        assert bot is not None, "首次构造宵禁调度器须传入 bot"
        assert locale_resolver is not None, "首次构造宵禁调度器须传入 locale_resolver"
        assert translator is not None, "首次构造宵禁调度器须传入 translator"
        _scheduler = CurfewScheduler(bot, locale_resolver, translator)
    return _scheduler
