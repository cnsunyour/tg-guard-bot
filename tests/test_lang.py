"""LocalePreferenceService 写穿语义与 /lang handler 核心契约测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.filters import CommandObject
from aiogram.types import Message

from src.bot.handlers import lang as lang_module
from src.core.i18n.resolver import LocalePreferenceCache, LocaleResolver
from src.core.i18n.translator import Translator
from src.services.locale_preference import LocalePreferenceService

pytestmark = pytest.mark.unit

_TEST_TTL = 123


def _make_resolver() -> LocaleResolver:
    """不依赖运行环境 locale 配置的 resolver"""
    resolver = LocaleResolver(LocalePreferenceCache(ttl_seconds=_TEST_TTL))
    resolver.default_locale = "zh-Hans"
    resolver.supported_locales = {"zh-Hans", "zh-Hant", "en"}
    return resolver


def _make_translator() -> Translator:
    """含 /lang 所需 key 的小 catalog（三种语言）"""
    base_keys = {
        "lang.menu.current.message": "当前：{locale_name}",
        "lang.locale.zh_hans.button": "简体中文",
        "lang.locale.zh_hant.button": "繁體中文",
        "lang.locale.en.button": "English",
        "lang.option.selected.button": "✅ {locale_name}",
        "lang.change.permission_denied.toast": "❌ 无权限",
        "lang.change.saved.toast": "✅ 已切换 {locale_name}",
        "lang.change.invalid_data.toast": "❌ 无效",
        "lang.change.invalid_locale.toast": "❌ 不支持",
        "lang.change.save_failed.toast": "❌ 失败",
    }
    en_keys = {
        "lang.menu.current.message": "Current: {locale_name}",
        "lang.locale.zh_hans.button": "Simplified Chinese",
        "lang.locale.zh_hant.button": "Traditional Chinese",
        "lang.locale.en.button": "English",
        "lang.option.selected.button": "✅ {locale_name}",
        "lang.change.permission_denied.toast": "❌ No permission",
        "lang.change.saved.toast": "✅ Switched {locale_name}",
        "lang.change.invalid_data.toast": "❌ Invalid",
        "lang.change.invalid_locale.toast": "❌ Unsupported",
        "lang.change.save_failed.toast": "❌ Failed",
    }
    hant_keys = {
        "lang.menu.current.message": "目前：{locale_name}",
        "lang.locale.zh_hans.button": "简体中文",
        "lang.locale.zh_hant.button": "繁體中文",
        "lang.locale.en.button": "English",
        "lang.option.selected.button": "✅ {locale_name}",
        "lang.change.permission_denied.toast": "❌ 無權限",
        "lang.change.saved.toast": "✅ 已切換 {locale_name}",
        "lang.change.invalid_data.toast": "❌ 無效",
        "lang.change.invalid_locale.toast": "❌ 不支援",
        "lang.change.save_failed.toast": "❌ 失敗",
    }
    return Translator(
        {"zh-Hans": base_keys, "zh-Hant": hant_keys, "en": en_keys},
        default_locale="zh-Hans",
    )


def _make_message(
    *,
    chat_type: ChatType,
    chat_id: int,
    user_id: int = 42,
    sender_chat: object | None = None,
) -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = chat_id
    message.chat.title = "Test" if chat_type in {ChatType.GROUP, ChatType.SUPERGROUP} else None
    message.from_user = SimpleNamespace(id=user_id)
    message.sender_chat = sender_chat
    message.text = "/lang"
    message.answer = AsyncMock(return_value=MagicMock(spec=Message))
    return message


def _make_command(args: str | None) -> CommandObject:
    return CommandObject(command="lang", args=args)


# ==================== LocalePreferenceService 写穿语义 ====================


async def test_service_set_group_locale_success_returns_true(mocker) -> None:
    """DB 成功 + 缓存成功 → True"""
    mocker.patch.object(
        lang_module.GroupRepository, "update_locale", new=AsyncMock(return_value=True)
    )
    resolver = _make_resolver()
    resolver.cache.set_group = AsyncMock(return_value=True)

    assert await LocalePreferenceService(resolver).set_group_locale(-100, "en") is True


async def test_service_set_group_locale_cache_failure_still_returns_true(mocker) -> None:
    """DB 成功但缓存写失败 → 仍 True（DB 是权威，缓存依赖 TTL 收敛）"""
    mocker.patch.object(
        lang_module.GroupRepository, "update_locale", new=AsyncMock(return_value=True)
    )
    resolver = _make_resolver()
    resolver.cache.set_group = AsyncMock(return_value=False)

    assert await LocalePreferenceService(resolver).set_group_locale(-100, "en") is True


async def test_service_set_group_locale_db_returns_false(mocker) -> None:
    """群组不存在（update_locale 返回 False）→ False，不写缓存"""
    mocker.patch.object(
        lang_module.GroupRepository, "update_locale", new=AsyncMock(return_value=False)
    )
    resolver = _make_resolver()
    resolver.cache.set_group = AsyncMock()

    assert await LocalePreferenceService(resolver).set_group_locale(-100, "en") is False
    resolver.cache.set_group.assert_not_awaited()


async def test_service_set_group_locale_unsupported_locale() -> None:
    """非法 locale → 直接 False，不查 DB"""
    resolver = _make_resolver()

    assert await LocalePreferenceService(resolver).set_group_locale(-100, "fr") is False


async def test_service_set_user_locale_success_returns_true(mocker) -> None:
    upsert = mocker.patch.object(
        lang_module.UserSettingsRepository, "upsert_locale", new=AsyncMock(return_value=None)
    )
    resolver = _make_resolver()
    resolver.cache.set_user = AsyncMock(return_value=True)

    assert await LocalePreferenceService(resolver).set_user_locale(42, "zh-Hant") is True
    upsert.assert_awaited_once_with(42, "zh-Hant")


# ==================== 选中态直接查 DB ====================


async def test_read_group_locale_queries_db(mocker) -> None:
    mocker.patch.object(
        lang_module.GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(locale="zh-Hant")),
    )
    assert await lang_module._read_group_locale(-100, _make_resolver()) == "zh-Hant"


async def test_read_group_locale_normalizes_historical_alias(mocker) -> None:
    mocker.patch.object(
        lang_module.GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(locale="zh-HK")),
    )

    assert await lang_module._read_group_locale(-100, _make_resolver()) == "zh-Hant"


async def test_read_group_locale_missing_group_returns_default(mocker) -> None:
    mocker.patch.object(lang_module.GroupRepository, "get", new=AsyncMock(return_value=None))
    assert await lang_module._read_group_locale(-100, _make_resolver()) == "zh-Hans"


async def test_read_user_locale_missing_record_returns_default(mocker) -> None:
    mocker.patch.object(
        lang_module.UserSettingsRepository, "get_locale", new=AsyncMock(return_value=None)
    )
    assert await lang_module._read_user_locale(42, _make_resolver()) == "zh-Hans"


async def test_read_user_locale_normalizes_historical_alias(mocker) -> None:
    mocker.patch.object(
        lang_module.UserSettingsRepository,
        "get_locale",
        new=AsyncMock(return_value="en-GB"),
    )

    assert await lang_module._read_user_locale(42, _make_resolver()) == "en"


# ==================== _parse_locale_arg 参数解析 ====================


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("en", "en"),
        (" en zh ", "en"),
        ("zh-HK", "zh-Hant"),
        ("en-GB", "en"),
    ],
)
def test_parse_locale_arg_normalizes_first_token(args: str, expected: str) -> None:
    assert lang_module._parse_locale_arg(args, _make_resolver()) == expected


def test_parse_locale_arg_invalid_returns_none() -> None:
    assert lang_module._parse_locale_arg("xyz", _make_resolver()) is None


# ==================== cmd_lang message 路径 ====================


async def test_cmd_lang_group_uses_message_permission_helper(mocker) -> None:
    """群 /lang 走 message 版权限检查（含匿名管理员），通过则发菜单"""
    permission = mocker.patch.object(
        lang_module, "check_admin_permission", new=AsyncMock(return_value=True)
    )
    mocker.patch.object(lang_module.GroupRepository, "get_or_create", new=AsyncMock())
    mocker.patch.object(lang_module, "_read_group_locale", new=AsyncMock(return_value="en"))
    send_menu = mocker.patch.object(lang_module, "_send_menu", new=AsyncMock())

    message = _make_message(chat_type=ChatType.SUPERGROUP, chat_id=-100)
    bot = MagicMock()
    translator = _make_translator()
    sent = MagicMock(spec=Message)
    send_menu.return_value = sent

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command(None),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is sent
    permission.assert_awaited_once_with(message, bot)
    send_menu.assert_awaited_once()


async def test_cmd_lang_private_without_args_keeps_menu_behavior(mocker) -> None:
    """私聊无参数仍显示语言菜单，不执行持久化。"""
    permission = mocker.patch.object(
        lang_module, "check_admin_permission", new=AsyncMock(return_value=True)
    )
    mocker.patch.object(
        lang_module.UserSettingsRepository,
        "get_locale",
        new=AsyncMock(return_value=None),
    )
    send_menu = mocker.patch.object(lang_module, "_send_menu", new=AsyncMock())
    sent = MagicMock(spec=Message)
    send_menu.return_value = sent

    message = _make_message(chat_type=ChatType.PRIVATE, chat_id=42)
    bot = MagicMock()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command(None),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is sent
    permission.assert_not_awaited()
    send_menu.assert_awaited_once()


async def test_cmd_lang_group_admin_direct_switch_returns_answer_message(mocker) -> None:
    """群管理员 /lang en 直接切换，并返回 message.answer 的 Message。"""
    mocker.patch.object(lang_module, "check_admin_permission", new=AsyncMock(return_value=True))
    get_or_create = mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(chat_type=ChatType.SUPERGROUP, chat_id=-100)
    message.text = "/lang en"
    bot = MagicMock()
    resolver = _make_resolver()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("en"),
        resolver,
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    get_or_create.assert_awaited_once_with(-100, "Test")
    persist.assert_awaited_once_with(
        bot,
        scope="group",
        chat_id=-100,
        user_id=42,
        locale="en",
        resolver=resolver,
        translator=translator,
        message_chat_id=-100,
        is_group=True,
    )
    message.answer.assert_awaited_once()
    assert "Switched" in message.answer.await_args.args[0]


async def test_cmd_lang_group_non_admin_direct_switch_denied(mocker) -> None:
    """群非管理员 /lang en 返回 permission_denied，不写入。"""
    permission = mocker.patch.object(
        lang_module, "check_admin_permission", new=AsyncMock(return_value=False)
    )
    get_or_create = mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(chat_type=ChatType.SUPERGROUP, chat_id=-100)
    message.text = "/lang en"
    bot = MagicMock()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("en"),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    permission.assert_awaited_once_with(message, bot)
    get_or_create.assert_not_awaited()
    persist.assert_not_awaited()
    assert "无权限" in message.answer.await_args.args[0]


async def test_cmd_lang_group_non_admin_invalid_locale_still_denied(mocker) -> None:
    """群非管理员 /lang xyz 仍返回 permission_denied，不暴露 locale 校验结果。"""
    permission = mocker.patch.object(
        lang_module, "check_admin_permission", new=AsyncMock(return_value=False)
    )
    parse_locale = mocker.patch.object(lang_module, "_parse_locale_arg")
    get_or_create = mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(chat_type=ChatType.SUPERGROUP, chat_id=-100)
    message.text = "/lang xyz"
    bot = MagicMock()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("xyz"),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    permission.assert_awaited_once_with(message, bot)
    parse_locale.assert_not_called()
    get_or_create.assert_not_awaited()
    persist.assert_not_awaited()
    answer_text = message.answer.await_args.args[0]
    assert "无权限" in answer_text
    assert "不支持" not in answer_text


async def test_cmd_lang_group_admin_invalid_locale_returns_invalid_locale(mocker) -> None:
    """群管理员 /lang xyz 返回 invalid_locale，且不创建群记录。"""
    mocker.patch.object(lang_module, "check_admin_permission", new=AsyncMock(return_value=True))
    get_or_create = mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(chat_type=ChatType.SUPERGROUP, chat_id=-100)
    message.text = "/lang xyz"
    bot = MagicMock()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("xyz"),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    get_or_create.assert_not_awaited()
    persist.assert_not_awaited()
    assert "不支持" in message.answer.await_args.args[0]


async def test_cmd_lang_private_direct_switch(mocker) -> None:
    """私聊 /lang zh-Hant 写入用户 locale、同步私聊菜单并返回新语言确认。"""
    permission = mocker.patch.object(
        lang_module, "check_admin_permission", new=AsyncMock(return_value=True)
    )
    get_or_create = mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(chat_type=ChatType.PRIVATE, chat_id=42)
    message.text = "/lang zh-Hant"
    bot = MagicMock()
    resolver = _make_resolver()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("zh-Hant"),
        resolver,
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    permission.assert_not_awaited()
    get_or_create.assert_not_awaited()
    persist.assert_awaited_once_with(
        bot,
        scope="private",
        chat_id=None,
        user_id=42,
        locale="zh-Hant",
        resolver=resolver,
        translator=translator,
        message_chat_id=42,
        is_group=False,
    )
    assert "已切換" in message.answer.await_args.args[0]


async def test_cmd_lang_private_invalid_locale(mocker) -> None:
    """私聊 /lang xyz 返回 invalid_locale，不执行写入。"""
    permission = mocker.patch.object(
        lang_module, "check_admin_permission", new=AsyncMock(return_value=True)
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(chat_type=ChatType.PRIVATE, chat_id=42)
    message.text = "/lang xyz"
    bot = MagicMock()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("xyz"),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    permission.assert_not_awaited()
    persist.assert_not_awaited()
    assert "不支持" in message.answer.await_args.args[0]


async def test_cmd_lang_anonymous_admin_direct_switches_via_message_path(mocker) -> None:
    """匿名管理员 message 路径可直接切换，且不调用 by_id 权限检查。"""
    by_id = mocker.patch(
        "src.core.utils.check_admin_permission_by_id",
        new=AsyncMock(return_value=False),
    )
    get_or_create = mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(
        chat_type=ChatType.SUPERGROUP,
        chat_id=-100,
        sender_chat=SimpleNamespace(id=-100),
    )
    message.text = "/lang en"
    bot = MagicMock()
    resolver = _make_resolver()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("en"),
        resolver,
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    by_id.assert_not_awaited()
    get_or_create.assert_awaited_once_with(-100, "Test")
    persist.assert_awaited_once()
    assert "Switched" in message.answer.await_args.args[0]


# ==================== _persist_locale helper ====================


async def test_persist_locale_group_writes_and_syncs(mocker) -> None:
    set_group = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_group_locale",
        new=AsyncMock(return_value=True),
    )
    set_user = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_user_locale",
        new=AsyncMock(return_value=True),
    )
    sync = mocker.patch.object(
        lang_module,
        "sync_chat_commands",
        new=AsyncMock(),
    )

    bot = MagicMock()
    resolver = _make_resolver()
    translator = _make_translator()

    result = await lang_module._persist_locale(
        bot,
        scope="group",
        chat_id=-100,
        user_id=42,
        locale="en",
        resolver=resolver,
        translator=translator,
        message_chat_id=-100,
        is_group=True,
    )

    assert result is True
    set_group.assert_awaited_once_with(-100, "en")
    set_user.assert_not_awaited()
    sync.assert_awaited_once()
    assert sync.await_args.args[0] is bot
    assert sync.await_args.args[1].locale == "en"
    assert sync.await_args.kwargs == {"chat_id": -100, "is_group": True}


async def test_persist_locale_private_writes_and_syncs(mocker) -> None:
    set_group = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_group_locale",
        new=AsyncMock(return_value=True),
    )
    set_user = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_user_locale",
        new=AsyncMock(return_value=True),
    )
    sync = mocker.patch.object(
        lang_module,
        "sync_chat_commands",
        new=AsyncMock(),
    )

    bot = MagicMock()
    resolver = _make_resolver()
    translator = _make_translator()

    result = await lang_module._persist_locale(
        bot,
        scope="private",
        chat_id=None,
        user_id=42,
        locale="zh-Hant",
        resolver=resolver,
        translator=translator,
        message_chat_id=42,
        is_group=False,
    )

    assert result is True
    set_group.assert_not_awaited()
    set_user.assert_awaited_once_with(42, "zh-Hant")
    sync.assert_awaited_once()
    assert sync.await_args.args[0] is bot
    assert sync.await_args.args[1].locale == "zh-Hant"
    assert sync.await_args.kwargs == {"chat_id": 42, "is_group": False}


async def test_persist_locale_save_failure_does_not_sync(mocker) -> None:
    set_group = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_group_locale",
        new=AsyncMock(return_value=False),
    )
    sync = mocker.patch.object(
        lang_module,
        "sync_chat_commands",
        new=AsyncMock(),
    )

    result = await lang_module._persist_locale(
        MagicMock(),
        scope="group",
        chat_id=-100,
        user_id=42,
        locale="en",
        resolver=_make_resolver(),
        translator=_make_translator(),
        message_chat_id=-100,
        is_group=True,
    )

    assert result is False
    set_group.assert_awaited_once_with(-100, "en")
    sync.assert_not_awaited()


async def test_persist_locale_sync_failure_keeps_saved_result(mocker) -> None:
    mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_group_locale",
        new=AsyncMock(return_value=True),
    )
    sync = mocker.patch.object(
        lang_module,
        "sync_chat_commands",
        new=AsyncMock(side_effect=RuntimeError("sync unavailable")),
    )

    result = await lang_module._persist_locale(
        MagicMock(),
        scope="group",
        chat_id=-100,
        user_id=42,
        locale="en",
        resolver=_make_resolver(),
        translator=_make_translator(),
        message_chat_id=-100,
        is_group=True,
    )

    assert result is True
    sync.assert_awaited_once()


# ==================== on_lang_callback 主链与拒绝路径 ====================


def _make_group_callback(
    *, chat_id: int = -100, locale: str = "en", user_id: int = 42
) -> MagicMock:
    """构造群聊 lang:set callback mock"""
    callback = MagicMock()
    callback.data = f"lang:set:group:{chat_id}:{locale}"
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = MagicMock()
    callback.message.chat.type = ChatType.SUPERGROUP
    callback.message.chat.id = chat_id
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


async def test_on_lang_callback_group_writes_through_and_rerenders_new_locale(mocker) -> None:
    """群 callback：写穿 + 用新 locale edit_text + answer（不依赖旧 middleware localizer）"""
    mocker.patch.object(
        lang_module, "check_admin_permission_by_id", new=AsyncMock(return_value=True)
    )
    update_locale = mocker.patch.object(
        lang_module.GroupRepository, "update_locale", new=AsyncMock(return_value=True)
    )
    # 写穿后选中态查 DB → en
    mocker.patch.object(
        lang_module.GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(locale="en")),
    )

    resolver = _make_resolver()
    resolver.cache.set_group = AsyncMock(return_value=True)
    translator = _make_translator()
    # middleware 注入的是旧 locale（zh-Hans），handler 不应依赖它
    old_localizer = translator.for_locale("zh-Hans")
    callback = _make_group_callback(locale="en")

    await lang_module.on_lang_callback(callback, MagicMock(), resolver, translator, old_localizer)

    update_locale.assert_awaited_once_with(-100, "en")
    # edit_text 用新 locale en 渲染（菜单文案为英文）
    callback.message.edit_text.assert_awaited_once()
    edited_text = callback.message.edit_text.await_args.args[0]
    assert "Current: English" in edited_text
    # answer 用新 locale en 的 saved toast
    callback.answer.assert_awaited_once()
    answer_text = callback.answer.await_args.args[0]
    assert "Switched" in answer_text


# ==================== _edit_saved_menu 幂等与失败降级 ====================


def _make_saved_menu_callback(*, edit_side_effect: BaseException | None = None) -> MagicMock:
    """构造 _edit_saved_menu 所需的 callback mock（edit_text 可注入异常）"""
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=42)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock(side_effect=edit_side_effect)
    return callback


def _bad_request(message: str):
    """构造 TelegramBadRequest，屏蔽 aiogram 构造细节（method 接受字符串）"""
    from aiogram.exceptions import TelegramBadRequest

    return TelegramBadRequest(method="sendMessage", message=message)


class _LoguruCapture:
    """loguru sink，收集日志记录用于断言。

    loguru 不走标准 logging，pytest caplog 抓不到；用 logger.add 注册临时
    sink 在 with 作用域内收集记录，退出时 remove。
    """

    def __init__(self) -> None:
        self.records: list = []

    def __enter__(self):
        from loguru import logger

        self._handler_id = logger.add(self.records.append, level="DEBUG")
        return self

    def __exit__(self, *exc):
        from loguru import logger

        logger.remove(self._handler_id)
        return False

    def messages_at(self, level_no: int) -> list[str]:
        return [r.record["message"] for r in self.records if r.record["level"].no >= level_no]


async def test_edit_saved_menu_idempotent_not_modified_logs_debug_and_answers_saved(
    mocker,
) -> None:
    """幂等未变化（重复点击当前语言）→ debug 日志 + saved 成功 toast（DB 已保存，不弹错误）"""
    from loguru import logger as _loguru_logger

    DEBUG_NO = _loguru_logger.level("DEBUG").no
    WARNING_NO = _loguru_logger.level("WARNING").no

    mocker.patch.object(lang_module, "_read_user_locale", new=AsyncMock(return_value="en"))
    callback = _make_saved_menu_callback(edit_side_effect=_bad_request("message is not modified"))
    translator = _make_translator()

    with _LoguruCapture() as cap:
        await lang_module._edit_saved_menu(
            callback=callback,
            message=callback.message,
            resolver=_make_resolver(),
            translator=translator,
            scope="private",
            chat_id=None,
            requested_locale="en",
        )

    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()
    assert "Switched" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs.get("show_alert") is not True
    # 幂等走 debug，严禁 warning/error
    assert any("未变化" in m for m in cap.messages_at(DEBUG_NO))
    assert cap.messages_at(WARNING_NO) == []


async def test_edit_saved_menu_other_bad_request_logs_warning_and_answers_saved(
    mocker,
) -> None:
    """非幂等编辑失败（如消息被删）→ warning 日志 + saved 成功 toast（locale 已保存，不显示成失败）"""
    from loguru import logger as _loguru_logger

    WARNING_NO = _loguru_logger.level("WARNING").no
    ERROR_NO = _loguru_logger.level("ERROR").no

    mocker.patch.object(lang_module, "_read_user_locale", new=AsyncMock(return_value="en"))
    callback = _make_saved_menu_callback(edit_side_effect=_bad_request("message to edit not found"))
    translator = _make_translator()

    with _LoguruCapture() as cap:
        await lang_module._edit_saved_menu(
            callback=callback,
            message=callback.message,
            resolver=_make_resolver(),
            translator=translator,
            scope="private",
            chat_id=None,
            requested_locale="en",
        )

    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()
    assert "Switched" in callback.answer.await_args.args[0]
    assert any("locale 已保存" in m for m in cap.messages_at(WARNING_NO))
    # 非幂等失败严禁 error（原 bug 就是误报 ERROR）
    assert cap.messages_at(ERROR_NO) == []


async def test_edit_saved_menu_non_bad_request_not_treated_as_idempotent(mocker) -> None:
    """非 TelegramBadRequest 即使文本含 'not modified' 也不误判为幂等（走 warning 分支）"""
    from loguru import logger as _loguru_logger

    WARNING_NO = _loguru_logger.level("WARNING").no

    class FakeError(Exception):
        pass

    mocker.patch.object(lang_module, "_read_user_locale", new=AsyncMock(return_value="en"))
    callback = _make_saved_menu_callback(
        edit_side_effect=FakeError("something not modified something")
    )
    translator = _make_translator()

    with _LoguruCapture() as cap:
        await lang_module._edit_saved_menu(
            callback=callback,
            message=callback.message,
            resolver=_make_resolver(),
            translator=translator,
            scope="private",
            chat_id=None,
            requested_locale="en",
        )

    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()
    # 非 TelegramBadRequest 不误判为幂等 → 走 warning 分支（而非 debug 幂等分支）
    assert any("locale 已保存" in m for m in cap.messages_at(WARNING_NO))
    # 辅助函数分类正确性
    assert lang_module._is_message_not_modified(FakeError("not modified")) is False
    assert lang_module._is_message_not_modified(_bad_request("message is not modified")) is True


async def test_on_lang_callback_group_chat_mismatch_rejected(mocker) -> None:
    """callback_data 的 chat_id 与消息 chat 不一致 → invalid_data，不写穿"""
    is_admin = mocker.patch.object(
        lang_module, "check_admin_permission_by_id", new=AsyncMock(return_value=True)
    )
    update_locale = mocker.patch.object(
        lang_module.GroupRepository, "update_locale", new=AsyncMock(return_value=True)
    )

    resolver = _make_resolver()
    translator = _make_translator()
    old_localizer = translator.for_locale("zh-Hans")
    # callback_data 声称 chat=-200，但消息实际 chat=-100
    callback = _make_group_callback(chat_id=-200, locale="en")
    callback.message.chat.id = -100

    await lang_module.on_lang_callback(callback, MagicMock(), resolver, translator, old_localizer)

    update_locale.assert_not_awaited()
    is_admin.assert_not_awaited()  # chat 校验在管理员校验之前
    callback.answer.assert_awaited_once()
    assert "无效" in callback.answer.await_args.args[0]


async def test_on_lang_callback_group_non_admin_rejected(mocker) -> None:
    """群 callback 非管理员 → permission_denied"""
    mocker.patch.object(
        lang_module, "check_admin_permission_by_id", new=AsyncMock(return_value=False)
    )
    update_locale = mocker.patch.object(
        lang_module.GroupRepository, "update_locale", new=AsyncMock(return_value=True)
    )

    resolver = _make_resolver()
    translator = _make_translator()
    old_localizer = translator.for_locale("zh-Hans")
    callback = _make_group_callback(locale="en")

    await lang_module.on_lang_callback(callback, MagicMock(), resolver, translator, old_localizer)

    update_locale.assert_not_awaited()
    assert "无权限" in callback.answer.await_args.args[0]


# ==================== 防御分支与失败路径（codex review 补充建议）====================


async def test_persist_locale_unknown_scope_returns_false(mocker) -> None:
    """未知 scope → False，不写入、不同步（防御分支）"""
    set_group = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_group_locale",
        new=AsyncMock(return_value=True),
    )
    set_user = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_user_locale",
        new=AsyncMock(return_value=True),
    )
    sync = mocker.patch.object(
        lang_module,
        "sync_chat_commands",
        new=AsyncMock(),
    )

    result = await lang_module._persist_locale(
        MagicMock(),
        scope="unknown",
        chat_id=-100,
        user_id=42,
        locale="en",
        resolver=_make_resolver(),
        translator=_make_translator(),
        message_chat_id=-100,
        is_group=False,
    )

    assert result is False
    set_group.assert_not_awaited()
    set_user.assert_not_awaited()
    sync.assert_not_awaited()


async def test_persist_locale_group_missing_chat_id_returns_false(mocker) -> None:
    """群 scope 缺 chat_id → False，不写入、不同步（防御分支）"""
    set_group = mocker.patch.object(
        lang_module.LocalePreferenceService,
        "set_group_locale",
        new=AsyncMock(return_value=True),
    )
    sync = mocker.patch.object(
        lang_module,
        "sync_chat_commands",
        new=AsyncMock(),
    )

    result = await lang_module._persist_locale(
        MagicMock(),
        scope="group",
        chat_id=None,
        user_id=42,
        locale="en",
        resolver=_make_resolver(),
        translator=_make_translator(),
        message_chat_id=-100,
        is_group=True,
    )

    assert result is False
    set_group.assert_not_awaited()
    sync.assert_not_awaited()


async def test_cmd_lang_group_admin_get_or_create_failure_returns_save_failed(mocker) -> None:
    """群管理员 /lang en 但 get_or_create 失败 → save_failed，不调 _persist_locale"""
    mocker.patch.object(lang_module, "check_admin_permission", new=AsyncMock(return_value=True))
    get_or_create = mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(side_effect=Exception("db down")),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=True),
    )

    message = _make_message(chat_type=ChatType.SUPERGROUP, chat_id=-100)
    message.text = "/lang en"
    bot = MagicMock()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("en"),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    get_or_create.assert_awaited_once()
    persist.assert_not_awaited()
    assert "失败" in message.answer.await_args.args[0]


async def test_cmd_lang_group_admin_persist_failure_returns_save_failed(mocker) -> None:
    """群管理员 /lang en 但 _persist_locale 返回 False → save_failed"""
    mocker.patch.object(lang_module, "check_admin_permission", new=AsyncMock(return_value=True))
    mocker.patch.object(
        lang_module.GroupRepository,
        "get_or_create",
        new=AsyncMock(),
    )
    persist = mocker.patch.object(
        lang_module,
        "_persist_locale",
        new=AsyncMock(return_value=False),
    )

    message = _make_message(chat_type=ChatType.SUPERGROUP, chat_id=-100)
    message.text = "/lang en"
    bot = MagicMock()
    translator = _make_translator()

    result = await lang_module.cmd_lang(
        message,
        bot,
        _make_command("en"),
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    assert result is message.answer.return_value
    persist.assert_awaited_once()
    assert "失败" in message.answer.await_args.args[0]
