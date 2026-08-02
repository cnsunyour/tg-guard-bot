"""启动命令处理器"""

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.core.i18n import get_resolver, get_translator
from src.core.utils import escape_html

router = Router(name="start")


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot) -> None:
    """处理 /start 命令"""
    # 命令处理器中这些值应该总是存在
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)

    # 检查是否是验证启动
    if len(args) > 1 and args[1].startswith("verify_"):
        # 格式: verify_{chat_id}
        try:
            chat_id_str = args[1].replace("verify_", "")
            chat_id = int(chat_id_str)

            # 获取群组信息
            try:
                chat = await bot.get_chat(chat_id)
                chat_title = escape_html(chat.title) if chat.title else "群组"
            except Exception:
                chat_title = "群组"

            # 提示用户重新加入群组
            await message.answer(
                f"✅ <b>Bot 已启动成功！</b>\n\n"
                f"现在请重新加入群组 <b>{chat_title}</b>，您将收到验证消息。\n\n"
                f"💡 验证通过后即可正常发言。",
                parse_mode="HTML",
            )

            logger.info(f"用户 {user_id} 通过验证链接启动 Bot (群组: {chat_id})")

        except ValueError:
            # 解析失败，显示通用欢迎消息
            await show_welcome_message(message)
    else:
        # 正常启动，显示欢迎消息
        await show_welcome_message(message)


async def show_welcome_message(message: Message):
    """显示欢迎消息（按点击者的私聊语言偏好渲染）。

    保持简洁，不塞完整命令手册（完整列表属 /help → show_command_overview）。
    """
    if not message.from_user:
        return
    locale = await get_resolver().for_user(message.from_user.id)
    localizer = get_translator().for_locale(locale)
    await message.answer(
        localizer.t("start.welcome.private.message"),
        parse_mode="HTML",
    )
