"""系统事件处理器"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import ADMINISTRATOR, KICKED, MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated
from loguru import logger

from src.core.i18n import BoundLocalizer
from src.repositories.group_repo import GroupRepository

router = Router(name="events")

# 仅处理群组/超级群事件，避免私聊触发 my_chat_member 时被误判为「加入群组」
router.my_chat_member.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED >> MEMBER))
@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED >> ADMINISTRATOR))
async def on_bot_added_to_group(
    event: ChatMemberUpdated, bot: Bot, localizer: BoundLocalizer
) -> None:
    """处理 Bot 被添加到群组的事件"""
    chat = event.chat
    chat_id = chat.id
    chat_title = chat.title or str(chat_id)

    logger.info(f"Bot 被添加到群组: {chat_title} (ID: {chat_id})")

    # 检查群组是否在白名单中
    group = await GroupRepository.get_by_id(chat_id)

    if group and group.is_whitelisted:
        logger.info(f"群组 {chat_title} 在白名单中，继续提供服务")
        # 发送欢迎消息
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=localizer.t("events.bot_added.welcome.message"),
            )
        except Exception as e:
            logger.error(f"发送欢迎消息失败: {e}")
    else:
        # 不在白名单中，自动退出
        logger.warning(f"群组 {chat_title} 不在白名单中，准备退出")

        try:
            # 发送提示消息
            await bot.send_message(
                chat_id=chat_id,
                text=localizer.t("common.group.unauthorized.message", chat_id=chat_id),
            )
        except Exception as e:
            logger.debug(f"发送退出提示消息失败: {e}")

        # 等待一下让消息发送完成
        import asyncio

        await asyncio.sleep(2)

        try:
            # 退出群组
            await bot.leave_chat(chat_id)
            logger.info(f"已退出非白名单群组: {chat_title} (ID: {chat_id})")
        except Exception as e:
            logger.error(f"退出群组失败 {chat_id}: {e}")


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER >> KICKED))
@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=ADMINISTRATOR >> KICKED))
async def on_bot_removed_from_group(event: ChatMemberUpdated) -> None:
    """处理 Bot 被移出群组的事件"""
    chat = event.chat
    chat_id = chat.id
    chat_title = chat.title or str(chat_id)

    logger.info(f"Bot 被移出群组: {chat_title} (ID: {chat_id})")
