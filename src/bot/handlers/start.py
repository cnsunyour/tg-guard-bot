"""启动命令处理器"""

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

router = Router(name="start")


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot) -> None:
    """处理 /start 命令"""
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
                chat_title = chat.title or "群组"
            except Exception:
                chat_title = "群组"

            # 提示用户重新加入群组
            await message.answer(
                f"✅ **Bot 已启动成功！**\n\n"
                f"现在请重新加入群组 **{chat_title}**，您将收到验证消息。\n\n"
                f"💡 验证通过后即可正常发言。",
                parse_mode="Markdown",
            )

            logger.info(f"用户 {user_id} 通过验证链接启动 Bot (群组: {chat_id})")

        except ValueError:
            # 解析失败，显示通用欢迎消息
            await show_welcome_message(message)
    else:
        # 正常启动，显示欢迎消息
        await show_welcome_message(message)


async def show_welcome_message(message: Message):
    """显示欢迎消息"""
    await message.answer(
        "👋 **欢迎使用 Guard Bot！**\n\n"
        "🤖 本 Bot 提供以下功能：\n"
        "• 入群验证\n"
        "• 智能反垃圾\n"
        "• 群组管理\n\n"
        "加入群组后，我会在私聊中发送验证消息。\n\n"
        "使用 /help 查看更多命令。",
        parse_mode="Markdown",
    )
