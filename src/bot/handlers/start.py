"""启动命令处理器"""

import re
from typing import Literal

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.bot.handlers.verification import handle_verification_start
from src.core.i18n import get_resolver, get_translator

router = Router(name="start")

# /start verify_[join_request_]{chat_id} 的 deep-link 参数正则
# join_request flow 显式前缀；旧 verify_{chat_id} 兼容默认 join
_VERIFY_START_RE = re.compile(r"^verify_(?:(join_request)_)?(-?\d+)$")


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot) -> None:
    """处理 /start 命令"""
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)

    # 验证恢复 deep-link：verify_[join_request_]{chat_id}
    if len(args) > 1 and (match := _VERIFY_START_RE.fullmatch(args[1])):
        flow_hint: Literal["join", "join_request"] = "join_request" if match.group(1) else "join"
        chat_id = int(match.group(2))
        logger.info(f"用户 {user_id} 通过验证链接启动 Bot [群组:{chat_id}] [flow:{flow_hint}]")
        await handle_verification_start(message, bot, chat_id, flow_hint)
        return

    # 正常启动，显示欢迎消息
    await show_welcome_message(message)


async def show_welcome_message(message: Message):
    """显示欢迎消息（按消息所在 chat 的 locale 渲染）。

    私聊用点击者 ``for_user`` 偏好；群组用 ``for_group``（避免群里回复用私聊
    语言，codex 3c1-3 P2）。保持简洁，完整命令手册属 /help → show_command_overview。
    """
    if not message.from_user:
        return
    resolver = get_resolver()
    if message.chat.type == "private":
        locale = await resolver.for_user(message.from_user.id)
    else:
        locale = await resolver.for_group(message.chat.id)
    localizer = get_translator().for_locale(locale)
    await message.answer(
        localizer.t("start.welcome.private.message"),
        parse_mode="HTML",
    )
