"""宵禁模式调度器"""

import asyncio

from aiogram import Bot
from loguru import logger
from sqlalchemy import select

from src.core.database import get_db_session
from src.models.group import Group
from src.services.curfew import CurfewService


class CurfewScheduler:
    """宵禁模式调度器

    定期检查所有启用宵禁的群组，发送进入/退出通知
    """

    def __init__(self, bot: Bot):
        self.bot = bot
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
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
            result = await session.execute(select(Group).where(Group.curfew_enabled == True))
            groups = result.scalars().all()

        for group in groups:
            try:
                await self._check_group(group)
            except Exception as e:
                logger.error(f"检查群组 {group.id} 宵禁状态失败: {e}")

    async def _check_group(self, group: Group) -> None:
        """检查单个群组的宵禁状态"""
        is_in_curfew = CurfewService.is_in_curfew(group)
        entered, exited = await CurfewService.track_curfew_state(group.id, is_in_curfew)

        if entered:
            # 发送进入宵禁通知
            try:
                await self.bot.send_message(
                    chat_id=group.id,
                    text=(
                        f"🌙 <b>宵禁模式已启动</b>\n\n"
                        f"宵禁时间: {group.curfew_start_hour:02d}:{group.curfew_start_minute:02d} - "
                        f"{group.curfew_end_hour:02d}:{group.curfew_end_minute:02d} "
                        f"(UTC{group.curfew_timezone_offset:+d})\n\n"
                        f"📋 <b>限制规则:</b>\n"
                        f"• 活跃度 = 0: 无法发送任何消息\n"
                        f"• 活跃度 &lt; 10: 无法发送非文本消息（图片、视频、贴纸等）\n"
                        f"• 活跃度 &gt;= 10: 可正常发送消息\n\n"
                        f"💡 发送文本消息可增加活跃度（每条+1）"
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
                    text="☀️ <b>宵禁模式已结束</b>\n\n所有消息限制已解除，可正常发言。",
                    parse_mode="HTML",
                )
                logger.info(f"已发送宵禁退出通知 [群组:{group.id}]")
            except Exception as e:
                logger.error(f"发送宵禁退出通知失败 [群组:{group.id}]: {e}")


# 全局调度器实例
_scheduler: CurfewScheduler | None = None


def get_curfew_scheduler(bot: Bot) -> CurfewScheduler:
    """获取全局宵禁调度器实例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = CurfewScheduler(bot)
    return _scheduler
