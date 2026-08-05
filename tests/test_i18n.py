"""i18n catalog、翻译器、语言解析器与中间件单元测试。

覆盖 codex review 指出的核心风险点：
- catalog 校验（重复 key / parity / HTML 子集 / 占位符安全）
- translator 降级链（requested → zh-Hans → key）与复数、严格模式、渲染兜底
- resolver 显式偏好三态语义（FOUND / ABSENT / QUERY_ERROR）与 NX 防竞态
- middleware ContextVar 嵌套 reset 与各类事件的目的地解析
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message, Update

from src.bot.middlewares.locale import LocaleMiddleware
from src.core.i18n.catalog import (
    CatalogError,
    load_catalog,
    load_catalogs,
    validate_catalog_parity,
)
from src.core.i18n.context import current_locale, get_current_locale
from src.core.i18n.resolver import LocalePreferenceCache, LocaleResolver
from src.core.i18n.translator import BoundLocalizer, TranslationError, Translator
from src.core.redis import RedisKeys
from src.repositories.group_repo import GroupRepository
from src.repositories.user_settings_repo import UserSettingsRepository

pytestmark = pytest.mark.unit

_TEST_TTL_SECONDS = 123
_SUPPORTED_LOCALES = {"zh-Hans", "zh-Hant", "en"}


class _BrokenFormat:
    """用于验证运行期 __format__ 异常不会穿透非严格翻译器。"""

    def __format__(self, format_spec: str) -> str:
        raise RuntimeError("format boom")


def _write_catalog(path: Path, catalog: object) -> None:
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")


def _make_resolver() -> LocaleResolver:
    """构造不依赖运行环境 locale 配置的 resolver"""
    resolver = LocaleResolver(LocalePreferenceCache(ttl_seconds=_TEST_TTL_SECONDS))
    resolver.default_locale = "zh-Hans"
    resolver.supported_locales = set(_SUPPORTED_LOCALES)
    return resolver


def _make_translator(*, strict: bool = False) -> Translator:
    return Translator(
        {
            "zh-Hans": {
                "common.hello": "你好",
                "common.greeting": "你好，{name}",
                "moderation.warnings.count.one": "{count} 次警告（单数占位）",
                "moderation.warnings.count.other": "共 {count} 次警告",
            },
            "zh-Hant": {
                "common.hello": "你好",
                "common.greeting": "你好，{name}",
                "moderation.warnings.count.one": "{count} 次警告（單數佔位）",
                "moderation.warnings.count.other": "共 {count} 次警告",
            },
            "en": {
                "common.hello": "Hello",
                "common.greeting": "Hello, {name}",
                "moderation.warnings.count.one": "{count} warning",
                "moderation.warnings.count.other": "{count} warnings",
            },
        },
        strict=strict,
    )


def _make_middleware() -> tuple[LocaleMiddleware, MagicMock]:
    resolver = MagicMock(spec=LocaleResolver)
    resolver.for_group = AsyncMock(return_value="en")
    resolver.for_user = AsyncMock(return_value="zh-Hant")
    middleware = LocaleMiddleware(resolver, _make_translator())
    return middleware, resolver


def _make_message(chat_type: ChatType, *, chat_id: int, user_id: int = 42) -> MagicMock:
    message = MagicMock(spec=Message)
    message.chat = MagicMock()
    message.chat.type = chat_type
    message.chat.id = chat_id
    message.from_user = MagicMock()
    message.from_user.id = user_id
    return message


def _make_callback(message: MagicMock | None, *, user_id: int = 42) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = MagicMock()
    callback.from_user.id = user_id
    return callback


def _make_member_event(*, chat_id: int = -100, user_id: int = 42) -> MagicMock:
    event = MagicMock(spec=ChatMemberUpdated)
    event.chat = MagicMock()
    event.chat.id = chat_id
    event.chat.type = ChatType.SUPERGROUP
    event.from_user = MagicMock()
    event.from_user.id = user_id
    return event


def _make_join_request(*, chat_id: int = -100, user_id: int = 42) -> MagicMock:
    event = MagicMock(spec=ChatJoinRequest)
    event.chat = MagicMock()
    event.chat.id = chat_id
    event.chat.type = ChatType.SUPERGROUP
    event.from_user = MagicMock()
    event.from_user.id = user_id
    return event


@pytest.fixture
def redis(mocker) -> AsyncMock:
    """mock resolver 模块内的 get_redis，返回受控 AsyncMock"""
    client = AsyncMock()
    client.get.return_value = None
    mocker.patch("src.core.i18n.resolver.get_redis", return_value=client)
    return client


# ==================== catalog ====================


def test_load_catalog_rejects_duplicate_keys(tmp_path: Path) -> None:
    """重复 key 必须被 object_pairs_hook 拦截"""
    path = tmp_path / "zh-Hans.json"
    path.write_text('{"common.ok":"一","common.ok":"二"}', encoding="utf-8")

    with pytest.raises(CatalogError, match="重复 key"):
        load_catalog(path)


@pytest.mark.parametrize("key", ["message", "Common.message"])
def test_load_catalog_rejects_invalid_key(tmp_path: Path, key: str) -> None:
    """key 必须是小写点分、至少两段"""
    path = tmp_path / "zh-Hans.json"
    _write_catalog(path, {key: "文案"})

    with pytest.raises(CatalogError, match="非法翻译 key"):
        load_catalog(path)


def test_load_catalog_rejects_non_string_value(tmp_path: Path) -> None:
    path = tmp_path / "zh-Hans.json"
    _write_catalog(path, {"common.count": 1})

    with pytest.raises(CatalogError, match="值必须是字符串"):
        load_catalog(path)


@pytest.mark.parametrize("template", ["{obj.attr}", "{x!r}", "{n:02d}"])
def test_load_catalog_rejects_unsafe_placeholder(tmp_path: Path, template: str) -> None:
    """占位符仅允许简单命名，禁止属性访问 / conversion / format spec"""
    path = tmp_path / "zh-Hans.json"
    _write_catalog(path, {"common.value": template})

    with pytest.raises(CatalogError):
        load_catalog(path)


@pytest.mark.parametrize(
    "template",
    ["<script>alert(1)</script>", '<a onclick="alert(1)">链接</a>'],
)
def test_load_catalog_rejects_disallowed_html_tag_or_attribute(
    tmp_path: Path,
    template: str,
) -> None:
    path = tmp_path / "zh-Hans.json"
    _write_catalog(path, {"common.value": template})

    with pytest.raises(CatalogError):
        load_catalog(path)


def test_load_catalog_rejects_non_telegram_spoiler_class(tmp_path: Path) -> None:
    path = tmp_path / "zh-Hans.json"
    _write_catalog(path, {"common.value": '<span class="spoiler">内容</span>'})

    with pytest.raises(CatalogError, match="tg-spoiler"):
        load_catalog(path)


def test_load_catalog_rejects_code_class_without_language_prefix(tmp_path: Path) -> None:
    path = tmp_path / "zh-Hans.json"
    _write_catalog(path, {"common.value": '<code class="python">print()</code>'})

    with pytest.raises(CatalogError, match="language-"):
        load_catalog(path)


def test_validate_catalog_parity_rejects_missing_key() -> None:
    catalogs = {
        "zh-Hans": {"common.first": "一", "common.second": "二"},
        "en": {"common.first": "one"},
    }

    with pytest.raises(CatalogError, match="缺失"):
        validate_catalog_parity(catalogs, "zh-Hans")


def test_validate_catalog_parity_rejects_extra_key() -> None:
    catalogs = {
        "zh-Hans": {"common.first": "一"},
        "en": {"common.first": "one", "common.second": "two"},
    }

    with pytest.raises(CatalogError, match="多余"):
        validate_catalog_parity(catalogs, "zh-Hans")


def test_validate_catalog_parity_rejects_placeholder_mismatch() -> None:
    catalogs = {
        "zh-Hans": {"common.greeting": "你好，{name}"},
        "en": {"common.greeting": "Hello, {username}"},
    }

    with pytest.raises(CatalogError, match="占位符"):
        validate_catalog_parity(catalogs, "zh-Hans")


def test_load_catalogs_rejects_missing_source_catalog(tmp_path: Path) -> None:
    _write_catalog(tmp_path / "en.json", {"common.ready": "ready"})

    with pytest.raises(CatalogError, match="缺少翻译目录文件"):
        load_catalogs(tmp_path, ["zh-Hans", "en"], "zh-Hans")


def test_load_catalogs_rejects_default_outside_supported_locales(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="default_locale"):
        load_catalogs(tmp_path, ["en"], "zh-Hans")


def test_load_catalogs_loads_source_locale_first(tmp_path: Path, mocker) -> None:
    """源语言必须最先加载，其损坏应立即暴露"""
    locales = ["en", "zh-Hans", "zh-Hant"]
    for locale in locales:
        _write_catalog(tmp_path / f"{locale}.json", {})

    loaded: list[str] = []

    def fake_load_catalog(path: Path) -> dict[str, str]:
        loaded.append(path.name)
        return {"common.ready": "ok"}

    mocker.patch("src.core.i18n.catalog.load_catalog", side_effect=fake_load_catalog)

    catalogs = load_catalogs(tmp_path, locales, "zh-Hans")

    assert list(catalogs) == ["zh-Hans", "en", "zh-Hant"]
    assert loaded == ["zh-Hans.json", "en.json", "zh-Hant.json"]


# ==================== translator ====================


def test_translator_returns_requested_translation() -> None:
    assert _make_translator().t("common.hello", locale="en") == "Hello"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh", "zh-Hans"),
        ("zh-CN", "zh-Hans"),
        ("zh-cn", "zh-Hans"),
        ("zh-SG", "zh-Hans"),
        ("zh-TW", "zh-Hant"),
        ("zh-HK", "zh-Hant"),
        ("zh-MO", "zh-Hant"),
        ("zh-Hans-CN", "zh-Hans"),
        ("zh-Hant-TW", "zh-Hant"),
        ("ZH-hant-HK", "zh-Hant"),
        ("en-US", "en"),
        ("en-gb", "en"),
        ("en-AU", "en"),  # en-* 通配
        ("en-u-ca-gregory", "en"),  # BCP 47 Unicode 扩展
        ("en-x-private", "en"),  # BCP 47 私有用段
    ],
)
def test_translator_normalizes_supported_locale_aliases(locale: str, expected: str) -> None:
    assert _make_translator(strict=True).for_locale(locale).locale == expected


def test_translator_prefers_exact_catalog_before_alias() -> None:
    translator = Translator(
        {
            "zh-Hans": {"common.hello": "默认简体"},
            "zh-CN": {"common.hello": "大陆变体"},
        },
        strict=True,
    )

    assert translator.for_locale("zh-CN").locale == "zh-CN"


def test_translator_strict_mode_rejects_unknown_chinese_variant() -> None:
    with pytest.raises(ValueError, match="不支持的 locale"):
        _make_translator(strict=True).for_locale("zh-Foo")


@pytest.mark.parametrize("locale", ["en-中文", "zh-中文", "zh-Hant-中文", "zh-Foo"])
def test_translator_strict_mode_rejects_malformed_locale(locale: str) -> None:
    """Unicode 畸形与未知 zh-* 变体不被静默接受（isascii 拒 / 不兜底简体）。"""
    with pytest.raises(ValueError, match="不支持的 locale"):
        _make_translator(strict=True).for_locale(locale)


def test_translator_rejects_zh_private_use_script_token() -> None:
    """zh-CN-x-hant 不把私用段 hant 误判为 script（不映射 zh-Hant，按不支持处理）。"""
    with pytest.raises(ValueError, match="不支持的 locale"):
        _make_translator(strict=True).for_locale("zh-CN-x-hant")


def test_translator_falls_back_to_default_when_requested_key_is_missing() -> None:
    translator = Translator({"zh-Hans": {"common.only": "默认文案"}, "en": {}}, strict=False)

    assert translator.t("common.only", locale="en") == "默认文案"


def test_translator_returns_key_when_all_catalogs_miss() -> None:
    assert _make_translator().t("common.missing", locale="en") == "common.missing"


def test_translator_interpolates_named_placeholder() -> None:
    assert _make_translator().t("common.greeting", locale="en", name="Alice") == "Hello, Alice"


def test_translator_plural_english_one() -> None:
    assert _make_translator().tp("moderation.warnings.count", 1, locale="en") == "1 warning"


@pytest.mark.parametrize("count", [0, 2])
def test_translator_plural_english_other(count: int) -> None:
    assert (
        _make_translator().tp("moderation.warnings.count", count, locale="en")
        == f"{count} warnings"
    )


def test_translator_plural_chinese_always_uses_other() -> None:
    assert _make_translator().tp("moderation.warnings.count", 1, locale="zh-Hans") == "共 1 次警告"


def test_translator_plural_injects_count_automatically() -> None:
    assert _make_translator().tp("moderation.warnings.count", 7, locale="en") == "7 warnings"


def test_translator_strict_mode_raises_for_missing_key() -> None:
    with pytest.raises(KeyError, match="缺少翻译 key"):
        _make_translator(strict=True).t("common.missing", locale="en")


def test_translator_strict_mode_raises_for_missing_variable() -> None:
    with pytest.raises(TranslationError, match="缺少模板变量"):
        _make_translator(strict=True).t("common.greeting", locale="en")


def test_translator_strict_mode_raises_for_unsupported_locale() -> None:
    with pytest.raises(ValueError, match="不支持的 locale"):
        _make_translator(strict=True).t("common.hello", locale="fr")


def test_translator_non_strict_missing_variable_falls_back_to_default() -> None:
    """非严格模式下缺变量应降级到默认语言，而非抛异常"""
    translator = Translator(
        {"zh-Hans": {"common.value": "默认文案"}, "en": {"common.value": "Value: {value}"}},
        strict=False,
    )

    assert translator.t("common.value", locale="en") == "默认文案"


def test_translator_non_strict_render_error_falls_back_to_default() -> None:
    """__format__ 抛错时非严格模式应兜底降级，不向调用方传播"""
    translator = Translator(
        {"zh-Hans": {"common.value": "默认文案"}, "en": {"common.value": "Value: {value}"}},
        strict=False,
    )

    assert translator.t("common.value", locale="en", value=_BrokenFormat()) == "默认文案"


def test_bound_localizer_uses_bound_locale() -> None:
    localizer = _make_translator().for_locale("zh-Hant")

    assert localizer.t("common.greeting", name="Alice") == "你好，Alice"
    assert isinstance(localizer, BoundLocalizer)


# ==================== resolver / cache ====================


async def test_resolver_group_returns_valid_cache_without_querying_db(
    redis: AsyncMock, mocker
) -> None:
    redis.get.return_value = "en"
    group_get = mocker.patch.object(GroupRepository, "get", new=AsyncMock())

    assert await _make_resolver().for_group(-100) == "en"
    group_get.assert_not_awaited()


async def test_resolver_group_normalizes_locale_alias_from_cache(redis: AsyncMock, mocker) -> None:
    redis.get.return_value = "zh-CN"
    group_get = mocker.patch.object(GroupRepository, "get", new=AsyncMock())

    assert await _make_resolver().for_group(-100) == "zh-Hans"
    group_get.assert_not_awaited()


async def test_resolver_group_invalid_cache_is_invalidated_and_reloaded(
    redis: AsyncMock, mocker
) -> None:
    redis.get.return_value = "invalid"
    group_get = mocker.patch.object(
        GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(locale="zh-Hant")),
    )

    assert await _make_resolver().for_group(-100) == "zh-Hant"
    redis.delete.assert_awaited_once_with(RedisKeys.locale_group(-100))
    group_get.assert_awaited_once_with(-100)


async def test_resolver_group_cache_miss_backfills_with_set_nx_ex(redis: AsyncMock, mocker) -> None:
    """cache miss 回填必须用 SET NX EX，不能覆盖并发 /lang 的权威写入"""
    redis.get.return_value = None
    mocker.patch.object(
        GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(locale="en")),
    )

    assert await _make_resolver().for_group(-100) == "en"
    redis.set.assert_awaited_once_with(
        RedisKeys.locale_group(-100),
        "en",
        nx=True,
        ex=_TEST_TTL_SECONDS,
    )
    redis.setex.assert_not_awaited()


async def test_resolver_group_missing_in_db_falls_back_to_default(redis: AsyncMock, mocker) -> None:
    redis.get.return_value = None
    mocker.patch.object(GroupRepository, "get", new=AsyncMock(return_value=None))

    assert await _make_resolver().for_group(-100) == "zh-Hans"


async def test_resolver_user_without_record_returns_default(redis: AsyncMock, mocker) -> None:
    redis.get.return_value = None
    mocker.patch.object(UserSettingsRepository, "get_locale", new=AsyncMock(return_value=None))

    assert await _make_resolver().for_user(42) == "zh-Hans"


@pytest.mark.parametrize("locale", ["zh-Hans", "zh-Hant"])
async def test_resolver_user_returns_explicit_supported_locale(
    locale: str, redis: AsyncMock, mocker
) -> None:
    redis.get.return_value = None
    mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(return_value=locale),
    )

    assert await _make_resolver().for_user(42) == locale


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("zh-TW", "zh-Hant"),
        ("en-US", "en"),
    ],
)
async def test_resolver_user_normalizes_db_alias_and_caches_canonical_locale(
    stored: str,
    expected: str,
    redis: AsyncMock,
    mocker,
) -> None:
    redis.get.return_value = None
    mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(return_value=stored),
    )

    assert await _make_resolver().for_user(42) == expected
    redis.set.assert_awaited_once_with(
        RedisKeys.locale_user(42),
        expected,
        nx=True,
        ex=_TEST_TTL_SECONDS,
    )


async def test_resolver_user_explicit_returns_none_for_missing_record(
    redis: AsyncMock, mocker
) -> None:
    """确认无记录返回 None，并缓存哨兵防穿透"""
    redis.get.return_value = None
    mocker.patch.object(UserSettingsRepository, "get_locale", new=AsyncMock(return_value=None))

    assert await _make_resolver().for_user_explicit(42) is None
    redis.set.assert_awaited_once_with(
        RedisKeys.locale_user(42),
        "",
        nx=True,
        ex=_TEST_TTL_SECONDS,
    )


async def test_resolver_user_explicit_preserves_explicit_default_locale(
    redis: AsyncMock, mocker
) -> None:
    """显式选择默认语言（zh-Hans）不得被当作无记录"""
    redis.get.return_value = None
    mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(return_value="zh-Hans"),
    )

    assert await _make_resolver().for_user_explicit(42) == "zh-Hans"


async def test_private_from_group_explicit_default_wins_group_locale(
    redis: AsyncMock, mocker
) -> None:
    """选项 B 核心：用户显式 zh-Hans 不得被英文群语言覆盖"""
    redis.get.side_effect = lambda key: "zh-Hans" if key == RedisKeys.locale_user(42) else "en"
    group_get = mocker.patch.object(GroupRepository, "get", new=AsyncMock())

    assert await _make_resolver().for_private_from_group(42, -100) == "zh-Hans"
    group_get.assert_not_awaited()


async def test_private_from_group_without_user_record_uses_group_locale(
    redis: AsyncMock, mocker
) -> None:
    """用户确认无记录 → 使用来源群语言"""
    redis.get.side_effect = lambda key: "" if key == RedisKeys.locale_user(42) else "en"
    user_get = mocker.patch.object(UserSettingsRepository, "get_locale", new=AsyncMock())
    group_get = mocker.patch.object(GroupRepository, "get", new=AsyncMock())

    assert await _make_resolver().for_private_from_group(42, -100) == "en"
    user_get.assert_not_awaited()
    group_get.assert_not_awaited()


async def test_private_from_group_explicit_non_default_wins_group_locale(
    redis: AsyncMock, mocker
) -> None:
    redis.get.side_effect = lambda key: "zh-Hant" if key == RedisKeys.locale_user(42) else "en"
    group_get = mocker.patch.object(GroupRepository, "get", new=AsyncMock())

    assert await _make_resolver().for_private_from_group(42, -100) == "zh-Hant"
    group_get.assert_not_awaited()


async def test_private_from_group_user_db_failure_falls_back_to_default(
    redis: AsyncMock, mocker
) -> None:
    """查询失败 ≠ 确认无记录：不得误用来源群语言，回退默认"""
    redis.get.return_value = None
    mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )
    mocker.patch.object(
        GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(locale="en")),
    )

    assert await _make_resolver().for_private_from_group(42, -100) == "zh-Hans"


async def test_resolver_redis_and_db_failures_fall_back_to_default(
    redis: AsyncMock, mocker
) -> None:
    redis.get.side_effect = RuntimeError("redis down")
    mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )
    mocker.patch.object(GroupRepository, "get", new=AsyncMock(side_effect=RuntimeError("db down")))

    assert await _make_resolver().for_private_from_group(42, -100) == "zh-Hans"


async def test_locale_cache_authoritative_write_uses_setex(redis: AsyncMock) -> None:
    """权威写（/lang 切换）必须用无条件 setex，保证覆盖"""
    cache = LocalePreferenceCache(ttl_seconds=_TEST_TTL_SECONDS)

    await cache.set_user(42, "en")

    redis.setex.assert_awaited_once_with(RedisKeys.locale_user(42), _TEST_TTL_SECONDS, "en")
    redis.set.assert_not_awaited()


async def test_resolver_user_invalid_cache_is_invalidated_and_reloaded(
    redis: AsyncMock, mocker
) -> None:
    redis.get.return_value = "invalid"
    user_get = mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(return_value="zh-Hant"),
    )

    assert await _make_resolver().for_user(42) == "zh-Hant"
    redis.delete.assert_awaited_once_with(RedisKeys.locale_user(42))
    user_get.assert_awaited_once_with(42)


async def test_resolver_redis_failure_still_falls_back_to_db(redis: AsyncMock, mocker) -> None:
    redis.get.side_effect = RuntimeError("redis down")
    mocker.patch.object(
        GroupRepository,
        "get",
        new=AsyncMock(return_value=SimpleNamespace(locale="en")),
    )

    assert await _make_resolver().for_group(-100) == "en"


# ==================== middleware ====================


async def test_middleware_resets_nested_context_even_when_handler_raises() -> None:
    """handler 抛异常也必须 reset ContextVar，恢复外层值"""
    middleware, _resolver = _make_middleware()
    message = _make_message(ChatType.SUPERGROUP, chat_id=-100)

    async def failing_handler(event, data):
        assert get_current_locale() == "en"
        raise RuntimeError("handler boom")

    outer_token = current_locale.set("zh-Hant")
    try:
        with pytest.raises(RuntimeError, match="handler boom"):
            await middleware(failing_handler, message, {})
        assert get_current_locale() == "zh-Hant"
    finally:
        current_locale.reset(outer_token)


async def test_middleware_group_message_uses_group_resolver() -> None:
    middleware, resolver = _make_middleware()

    await middleware(AsyncMock(), _make_message(ChatType.SUPERGROUP, chat_id=-100), {})

    resolver.for_group.assert_awaited_once_with(-100)
    resolver.for_user.assert_not_awaited()


async def test_middleware_private_message_uses_user_resolver() -> None:
    middleware, resolver = _make_middleware()

    await middleware(AsyncMock(), _make_message(ChatType.PRIVATE, chat_id=42, user_id=42), {})

    resolver.for_user.assert_awaited_once_with(42)
    resolver.for_group.assert_not_awaited()


async def test_middleware_group_callback_uses_message_chat_locale() -> None:
    middleware, resolver = _make_middleware()
    callback = _make_callback(_make_message(ChatType.SUPERGROUP, chat_id=-100), user_id=42)

    await middleware(AsyncMock(), callback, {})

    resolver.for_group.assert_awaited_once_with(-100)
    resolver.for_user.assert_not_awaited()


async def test_middleware_inline_callback_uses_clicker_locale() -> None:
    """无 message 的 inline callback 使用点击者偏好"""
    middleware, resolver = _make_middleware()
    callback = _make_callback(None, user_id=42)

    await middleware(AsyncMock(), callback, {})

    resolver.for_user.assert_awaited_once_with(42)
    resolver.for_group.assert_not_awaited()


async def test_middleware_chat_member_uses_group_resolver() -> None:
    middleware, resolver = _make_middleware()

    await middleware(AsyncMock(), _make_member_event(chat_id=-100), {})

    resolver.for_group.assert_awaited_once_with(-100)


async def test_middleware_chat_join_request_uses_group_resolver() -> None:
    middleware, resolver = _make_middleware()

    await middleware(AsyncMock(), _make_join_request(chat_id=-100), {})

    resolver.for_group.assert_awaited_once_with(-100)


async def test_middleware_injects_locale_and_bound_localizer() -> None:
    middleware, _resolver = _make_middleware()
    data: dict[str, object] = {}

    await middleware(AsyncMock(), _make_message(ChatType.SUPERGROUP, chat_id=-100), data)

    assert data["locale"] == "en"
    localizer = data["localizer"]
    assert isinstance(localizer, BoundLocalizer)
    assert localizer.locale == "en"


async def test_update_outer_middleware_routes_wrapped_message() -> None:
    """update outer 入口能正确解包 Update.message"""
    middleware, resolver = _make_middleware()
    update = MagicMock(spec=Update)
    update.message = _make_message(ChatType.SUPERGROUP, chat_id=-100)
    update.edited_message = None

    await middleware(AsyncMock(), update, {})

    resolver.for_group.assert_awaited_once_with(-100)


async def test_update_outer_middleware_routes_my_chat_member() -> None:
    middleware, resolver = _make_middleware()
    event = _make_member_event(chat_id=-100)
    update = MagicMock(spec=Update)
    update.message = None
    update.edited_message = None
    update.callback_query = None
    update.chat_member = None
    update.my_chat_member = event

    await middleware(AsyncMock(), update, {})

    resolver.for_group.assert_awaited_once_with(-100)


# ==================== review 补充：DB 非法值与 Update 路由 ====================


async def test_resolver_user_db_illegal_locale_treated_as_query_failure(
    redis: AsyncMock, mocker
) -> None:
    """DB 返回不支持的 locale 视为数据异常：降级默认且不写缓存，避免脏数据固化"""
    redis.get.return_value = None
    mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(return_value="fr"),
    )

    assert await _make_resolver().for_user(42) == "zh-Hans"
    # 不写任何缓存（既不写哨兵也不写非法值）
    redis.set.assert_not_awaited()
    redis.setex.assert_not_awaited()


async def test_resolver_user_explicit_db_failure_returns_default_without_caching(
    redis: AsyncMock, mocker
) -> None:
    """for_user_explicit 查询失败返回默认语言，且不写缓存（语义见 docstring 警告）"""
    redis.get.return_value = None
    mocker.patch.object(
        UserSettingsRepository,
        "get_locale",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )

    assert await _make_resolver().for_user_explicit(42) == "zh-Hans"
    redis.set.assert_not_awaited()
    redis.setex.assert_not_awaited()


async def test_update_outer_middleware_routes_callback_query() -> None:
    """Update.callback_query 经群消息 callback 路由到 for_group"""
    middleware, resolver = _make_middleware()
    callback = _make_callback(_make_message(ChatType.SUPERGROUP, chat_id=-100), user_id=42)
    update = MagicMock(spec=Update)
    update.message = None
    update.edited_message = None
    update.callback_query = callback

    await middleware(AsyncMock(), update, {})

    resolver.for_group.assert_awaited_once_with(-100)


async def test_update_outer_middleware_routes_chat_member() -> None:
    middleware, resolver = _make_middleware()
    event = _make_member_event(chat_id=-100)
    update = MagicMock(spec=Update)
    update.message = None
    update.edited_message = None
    update.callback_query = None
    update.chat_member = event
    update.my_chat_member = None

    await middleware(AsyncMock(), update, {})

    resolver.for_group.assert_awaited_once_with(-100)


async def test_update_outer_middleware_routes_chat_join_request() -> None:
    middleware, resolver = _make_middleware()
    event = _make_join_request(chat_id=-100)
    update = MagicMock(spec=Update)
    update.message = None
    update.edited_message = None
    update.callback_query = None
    update.chat_member = None
    update.my_chat_member = None
    update.chat_join_request = event

    await middleware(AsyncMock(), update, {})

    resolver.for_group.assert_awaited_once_with(-100)
