"""/lang 语言偏好命令与 inline keyboard 处理器

- 群聊：仅管理员可切换群 locale（统一管理员权限检查，含匿名管理员）
- 私聊：用户自助切换私聊 locale
- 选中态直接查 DB（不经过 for_user_explicit，避免查询失败语义混淆）
- 写穿后用「新 locale」的 BoundLocalizer 重渲染菜单，不依赖 middleware 注入的旧 localizer
"""

from collections.abc import Sequence

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from src.bot.commands import sync_chat_commands
from src.core.config import settings
from src.core.i18n.locales import normalize_supported_locale
from src.core.i18n.resolver import LocaleResolver
from src.core.i18n.translator import BoundLocalizer, Translator
from src.core.utils import check_admin_permission, check_admin_permission_by_id
from src.repositories.group_repo import GroupRepository
from src.repositories.user_settings_repo import UserSettingsRepository
from src.services.locale_preference import LocalePreferenceService

router = Router(name="lang")

_GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


def _locale_key(locale: str) -> str:
    """BCP 47 locale → catalog key 后缀（zh-Hans → zh_hans）"""
    return locale.lower().replace("-", "_")


def _supported_locales(resolver: LocaleResolver) -> Sequence[str]:
    """按配置顺序返回已加载的支持语言（决定菜单展示顺序）"""
    return tuple(
        locale for locale in settings.supported_locales if locale in resolver.supported_locales
    )


def _locale_name(localizer: BoundLocalizer, locale: str) -> str:
    """取某语言的 endonym（自称，固定不随当前 locale 变）。

    语言切换器最佳实践：按钮标签用目标语言的自称（endonym），即使界面是
    不认识的语言，用户也能认出自己想选的选项。因此 catalog 的
    lang.locale.*.button 三语同值（均为 endonym，不翻译）。
    """
    return localizer.t(f"lang.locale.{_locale_key(locale)}.button")


def _build_keyboard(
    localizer: BoundLocalizer,
    resolver: LocaleResolver,
    scope: str,
    chat_id: int | None,
    current_locale: str,
) -> InlineKeyboardMarkup:
    """构造语言选择菜单

    callback_data 只携带操作范围、群 ID（群场景）和目标 locale；
    用户 ID 一律从 callback.from_user 获取，不写进 callback_data。
    """
    rows: list[list[InlineKeyboardButton]] = []
    for locale in _supported_locales(resolver):
        name = _locale_name(localizer, locale)
        text = (
            localizer.t("lang.option.selected.button", locale_name=name)
            if locale == current_locale
            else name
        )
        if scope == "group":
            callback_data = f"lang:set:group:{chat_id}:{locale}"
        else:
            callback_data = f"lang:set:private:{locale}"
        rows.append([InlineKeyboardButton(text=text, callback_data=callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_menu(
    localizer: BoundLocalizer,
    resolver: LocaleResolver,
    scope: str,
    chat_id: int | None,
    current_locale: str,
) -> tuple[str, InlineKeyboardMarkup]:
    name = _locale_name(localizer, current_locale)
    text = localizer.t("lang.menu.current.message", locale_name=name)
    keyboard = _build_keyboard(
        localizer=localizer,
        resolver=resolver,
        scope=scope,
        chat_id=chat_id,
        current_locale=current_locale,
    )
    return text, keyboard


async def _read_group_locale(chat_id: int, resolver: LocaleResolver) -> str:
    """直接读群组 DB locale（选中态不用缓存旧值）"""
    group = await GroupRepository.get(chat_id)
    if group is None:
        return resolver.default_locale
    normalized = normalize_supported_locale(group.locale, resolver.supported_locales)
    if normalized is None:
        logger.error(f"群组 DB locale 非法，降级默认 [群组:{chat_id}] [locale:{group.locale}]")
        return resolver.default_locale
    return normalized


async def _read_user_locale(user_id: int, resolver: LocaleResolver) -> str:
    """直接读用户 DB locale；无记录用默认语言"""
    locale = await UserSettingsRepository.get_locale(user_id)
    if locale is None:
        return resolver.default_locale
    normalized = normalize_supported_locale(locale, resolver.supported_locales)
    if normalized is None:
        logger.error(f"用户 DB locale 非法，降级默认 [用户:{user_id}] [locale:{locale}]")
        return resolver.default_locale
    return normalized


async def _send_menu(
    message: Message,
    resolver: LocaleResolver,
    translator: Translator,
    scope: str,
    chat_id: int | None,
    current_locale: str,
) -> Message:
    localizer = translator.for_locale(current_locale)
    text, keyboard = _render_menu(
        localizer=localizer,
        resolver=resolver,
        scope=scope,
        chat_id=chat_id,
        current_locale=current_locale,
    )
    return await message.answer(text, reply_markup=keyboard)


@router.message(Command("lang"))
async def cmd_lang(
    message: Message,
    bot: Bot,
    locale_resolver: LocaleResolver,
    translator: Translator,
    localizer: BoundLocalizer,
) -> Message | None:
    """显示语言设置菜单

    返回发出的消息对象，使 AutoDeleteMiddleware 能在群组中自动删除命令响应。
    """
    if message.from_user is None:
        return None

    # 私聊：用户自助
    if message.chat.type == ChatType.PRIVATE:
        try:
            current = await _read_user_locale(message.from_user.id, locale_resolver)
            return await _send_menu(
                message,
                locale_resolver,
                translator,
                scope="private",
                chat_id=None,
                current_locale=current,
            )
        except Exception as exc:
            logger.error(f"读取用户语言设置失败 [用户:{message.from_user.id}]: {exc}")
            return await message.answer(localizer.t("lang.change.save_failed.toast"))

    # 非群聊类型不支持
    if message.chat.type not in _GROUP_TYPES:
        return await message.answer(localizer.t("lang.change.invalid_data.toast"))

    chat_id = message.chat.id
    if not await check_admin_permission(message, bot):
        return await message.answer(localizer.t("lang.change.permission_denied.toast"))

    try:
        # 确保群记录存在（update_locale 需既有记录）
        await GroupRepository.get_or_create(chat_id, message.chat.title)
        current = await _read_group_locale(chat_id, locale_resolver)
        return await _send_menu(
            message,
            locale_resolver,
            translator,
            scope="group",
            chat_id=chat_id,
            current_locale=current,
        )
    except Exception as exc:
        logger.error(f"读取群组语言设置失败 [群组:{chat_id}]: {exc}")
        return await message.answer(localizer.t("lang.change.save_failed.toast"))


async def _answer_invalid_data(callback: CallbackQuery, localizer: BoundLocalizer) -> None:
    await callback.answer(localizer.t("lang.change.invalid_data.toast"), show_alert=True)


async def _edit_saved_menu(
    callback: CallbackQuery,
    message: Message,
    resolver: LocaleResolver,
    translator: Translator,
    scope: str,
    chat_id: int | None,
    requested_locale: str,
) -> None:
    """保存成功后用「新 locale」重渲染菜单

    选中态从 DB 读取；翻译器显式绑定 requested_locale，不使用本次 callback
    middleware 中可能仍是旧值的 localizer，保证立即生效。
    """
    try:
        if scope == "group":
            current = await _read_group_locale(chat_id or 0, resolver)
        else:
            current = await _read_user_locale(callback.from_user.id, resolver)
    except Exception as exc:
        logger.warning(f"保存后读取语言状态失败，使用请求 locale 渲染: {exc}")
        current = requested_locale

    new_localizer = translator.for_locale(requested_locale)
    text, keyboard = _render_menu(
        localizer=new_localizer,
        resolver=resolver,
        scope=scope,
        chat_id=chat_id,
        current_locale=current,
    )

    try:
        await message.edit_text(text, reply_markup=keyboard)
    except Exception as exc:
        logger.error(f"更新语言菜单消息失败: {exc}")
        await callback.answer(
            new_localizer.t("lang.change.message_unavailable.toast"), show_alert=True
        )
        return

    await callback.answer(
        new_localizer.t(
            "lang.change.saved.toast",
            locale_name=_locale_name(new_localizer, requested_locale),
        )
    )


@router.callback_query(F.data.startswith("lang:set:"))
async def on_lang_callback(
    callback: CallbackQuery,
    bot: Bot,
    locale_resolver: LocaleResolver,
    translator: Translator,
    localizer: BoundLocalizer,
) -> None:
    """处理群组与私聊语言选择 callback"""
    if (
        not callback.data
        or callback.message is None
        or callback.from_user is None
        or isinstance(callback.message, InaccessibleMessage)
    ):
        await _answer_invalid_data(callback, localizer)
        return

    message = callback.message
    parts = callback.data.split(":")

    # 解析与基础校验 callback_data
    try:
        scope = parts[2]
        if scope == "group":
            if len(parts) != 5:
                raise ValueError("群组 callback 字段数量错误")
            chat_id = int(parts[3])
            locale = parts[4]
            # callback 来源 chat 必须与 callback_data 一致，且是群聊
            if message.chat.type not in _GROUP_TYPES or message.chat.id != chat_id:
                await _answer_invalid_data(callback, localizer)
                return
            if not await check_admin_permission_by_id(bot, chat_id, callback.from_user.id):
                await callback.answer(
                    localizer.t("lang.change.permission_denied.toast"), show_alert=True
                )
                return
        elif scope == "private":
            if len(parts) != 4:
                raise ValueError("私聊 callback 字段数量错误")
            chat_id = None
            locale = parts[3]
            # 私聊 callback 必须来自点击者本人的私聊
            if message.chat.type != ChatType.PRIVATE or message.chat.id != callback.from_user.id:
                await _answer_invalid_data(callback, localizer)
                return
        else:
            raise ValueError(f"未知语言设置范围: {scope}")
    except (IndexError, TypeError, ValueError) as exc:
        logger.warning(f"解析语言设置 callback 失败: {exc}")
        await _answer_invalid_data(callback, localizer)
        return

    if locale not in locale_resolver.supported_locales:
        await callback.answer(localizer.t("lang.change.invalid_locale.toast"), show_alert=True)
        return

    # 写穿：DB commit → 权威 setex（封装在 LocalePreferenceService）
    preference_service = LocalePreferenceService(locale_resolver)
    if scope == "group":
        saved = await preference_service.set_group_locale(chat_id or 0, locale)
    else:
        saved = await preference_service.set_user_locale(callback.from_user.id, locale)

    if not saved:
        await callback.answer(localizer.t("lang.change.save_failed.toast"), show_alert=True)
        return

    # locale 已持久化；命令菜单同步失败只记日志，不回滚 locale。
    # 不传 language_code，命令语言只由 Bot 内 locale 决定（独立于 Telegram 系统语言）。
    #
    # 已知限制（codex review P2）：同一 chat 并发 /lang 时，先 commit 的 callback
    # 可能在后 commit 的之后完成 sync，导致 Telegram 菜单与最终 DB locale 短暂不一致。
    # 恢复路径：启动 rehydrate_custom_locale_commands 会按 DB 权威 locale 重新 sync；
    # 或用户再点一次 /lang。per-chat 锁可彻底消除但 ROI 不足（极低频边缘场景）。
    await sync_chat_commands(
        bot,
        translator.for_locale(locale),
        chat_id=message.chat.id,
        is_group=scope == "group",
    )

    # 用新 locale 重渲染菜单 + toast，确保立即生效
    await _edit_saved_menu(
        callback=callback,
        message=message,
        resolver=locale_resolver,
        translator=translator,
        scope=scope,
        chat_id=chat_id,
        requested_locale=locale,
    )
