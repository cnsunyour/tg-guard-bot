"""当前 Update 的语言上下文中间件

作为 Dispatcher 的 update outer middleware 注册，使 message、callback_query、
chat_member、my_chat_member、chat_join_request 及其内部类型中间件共享
同一 ContextVar，从而 whitelist、throttle、CAS 等提前返回的路径也能取到 locale。

注意：ContextVar 只是「当前 Update 的便利默认值」。当发送目标是别的聊天
（如群流程触发的私聊）或来自定时/延迟任务时，必须用 LocaleResolver 显式解析。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatJoinRequest,
    ChatMemberUpdated,
    Message,
    TelegramObject,
    Update,
)

from src.core.config import settings
from src.core.i18n.context import current_locale
from src.core.i18n.resolver import LocaleResolver
from src.core.i18n.translator import Translator


class LocaleMiddleware(BaseMiddleware):
    """为一个完整 Update 解析并绑定 locale"""

    def __init__(self, resolver: LocaleResolver, translator: Translator) -> None:
        super().__init__()
        self.resolver = resolver
        self.translator = translator

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        locale = await self._resolve_event_locale(event)
        localizer = self.translator.for_locale(locale)

        token = current_locale.set(locale)
        data["locale"] = locale
        data["localizer"] = localizer
        # 注入共享依赖，handler 不自行构造 resolver/translator
        data["locale_resolver"] = self.resolver
        data["translator"] = self.translator
        try:
            return await handler(event, data)
        finally:
            # reset(token) 恢复外层上下文；不能 set(default)，否则嵌套调用会错乱
            current_locale.reset(token)

    async def _resolve_event_locale(self, event: TelegramObject) -> str:
        # 主路径：update outer middleware 收到的 event 总是 Update
        if isinstance(event, Update):
            return await self._resolve_update(event)

        # 兼容单元测试：允许直接传入具体事件类型
        if isinstance(event, Message):
            return await self._resolve_chat(event.chat, self._user_id(event))
        if isinstance(event, CallbackQuery):
            return await self._resolve_callback(event)
        if isinstance(event, ChatMemberUpdated):
            return await self._resolve_chat(
                event.chat, event.from_user.id if event.from_user else None
            )
        if isinstance(event, ChatJoinRequest):
            return await self._resolve_chat(event.chat, event.from_user.id)

        return settings.default_locale

    async def _resolve_update(self, update: Update) -> str:
        # 普通消息与编辑消息都可能触发用户可见响应
        message = update.message or update.edited_message
        if message is not None:
            return await self._resolve_chat(message.chat, self._user_id(message))
        if update.callback_query is not None:
            return await self._resolve_callback(update.callback_query)

        # chat_member / my_chat_member 均以群组为目标，使用群语言
        member_event = update.chat_member or update.my_chat_member
        if member_event is not None:
            return await self._resolve_chat(
                member_event.chat,
                member_event.from_user.id if member_event.from_user else None,
            )

        if update.chat_join_request is not None:
            request = update.chat_join_request
            return await self._resolve_chat(request.chat, request.from_user.id)

        return settings.default_locale

    async def _resolve_callback(self, callback: CallbackQuery) -> str:
        # 群消息下的 callback 属于群 UI，使用群语言
        if callback.message is not None:
            return await self._resolve_chat(callback.message.chat, callback.from_user.id)
        # inline message 等无 chat 的 callback，使用点击者偏好
        return await self.resolver.for_user(callback.from_user.id)

    async def _resolve_chat(self, chat: Chat | None, user_id: int | None) -> str:
        if chat is None:
            return await self.resolver.for_user(user_id) if user_id else settings.default_locale
        if chat.type == ChatType.PRIVATE:
            return await self.resolver.for_user(user_id or chat.id)
        return await self.resolver.for_group(chat.id)

    @staticmethod
    def _user_id(message: Message) -> int | None:
        return message.from_user.id if message.from_user else None
