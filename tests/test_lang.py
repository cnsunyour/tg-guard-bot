"""LocalePreferenceService 写穿语义与 /lang handler 核心契约测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType

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
    """含 /lang 所需 key 的小 catalog（zh-Hans + en）"""
    base_keys = {
        "lang.menu.current.message": "当前：{locale_name}",
        "lang.locale.zh_hans.button": "简体中文",
        "lang.locale.zh_hant.button": "繁体中文",
        "lang.locale.en.button": "英语",
        "lang.option.selected.button": "✅ {locale_name}",
        "lang.change.permission_denied.toast": "❌ 无权限",
        "lang.change.saved.toast": "✅ 已切换 {locale_name}",
        "lang.change.invalid_data.toast": "❌ 无效",
        "lang.change.invalid_locale.toast": "❌ 不支持",
        "lang.change.save_failed.toast": "❌ 失败",
        "lang.change.message_unavailable.toast": "❌ 无法更新",
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
        "lang.change.message_unavailable.toast": "❌ Unavailable",
    }
    return Translator({"zh-Hans": base_keys, "en": en_keys}, default_locale="zh-Hans")


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


async def test_cmd_lang_group_uses_message_permission_helper(mocker) -> None:
    """群 /lang 走 message 版权限检查（含匿名管理员），通过则发菜单"""
    permission = mocker.patch.object(
        lang_module, "check_admin_permission", new=AsyncMock(return_value=True)
    )
    mocker.patch.object(lang_module.GroupRepository, "get_or_create", new=AsyncMock())
    mocker.patch.object(lang_module, "_read_group_locale", new=AsyncMock(return_value="en"))
    send_menu = mocker.patch.object(lang_module, "_send_menu", new=AsyncMock())

    message = MagicMock()
    message.chat.type = ChatType.SUPERGROUP
    message.chat.id = -100
    message.chat.title = "Test"
    message.from_user = SimpleNamespace(id=42)
    bot = MagicMock()
    translator = _make_translator()

    await lang_module.cmd_lang(
        message,
        bot,
        _make_resolver(),
        translator,
        translator.for_locale("zh-Hans"),
    )

    permission.assert_awaited_once_with(message, bot)
    send_menu.assert_awaited_once()


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
