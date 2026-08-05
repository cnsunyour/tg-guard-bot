"""纯同步翻译器

Translator 不访问数据库、Redis 或文件系统。locale 解析由 LocaleResolver
负责，catalog 加载由 catalog 模块负责。本类只做内存查表与模板渲染。
"""

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.core.i18n.catalog import extract_placeholders
from src.core.i18n.context import get_current_locale
from src.core.i18n.locales import normalize_supported_locale

# 复数分类：当前支持的语言仅需 one/other。未来加入俄语、阿拉伯语等
# 复杂复数语言时，再切换为 CLDR 分类器。
_PLURAL_CATEGORY_ONE = "one"
_PLURAL_CATEGORY_OTHER = "other"


class TranslationError(RuntimeError):
    """翻译渲染错误（严格模式下抛出）"""


class Translator:
    """内存 JSON catalog 翻译器"""

    # 同一 (kind, locale, key) 的错误日志最少间隔，避免洪泛
    LOG_INTERVAL_SECONDS = 300.0

    def __init__(
        self,
        catalogs: Mapping[str, Mapping[str, str]],
        default_locale: str = "zh-Hans",
        strict: bool = False,
    ) -> None:
        if default_locale not in catalogs:
            raise ValueError(f"缺少默认语言 catalog: {default_locale}")

        self._catalogs: dict[str, dict[str, str]] = {
            locale: dict(catalog) for locale, catalog in catalogs.items()
        }
        # 预解析每个 key 的占位符集合，避免运行时重复 extract
        self._placeholders: dict[str, dict[str, frozenset[str]]] = {
            locale: {key: extract_placeholders(template) for key, template in catalog.items()}
            for locale, catalog in self._catalogs.items()
        }
        self.default_locale = default_locale
        self.strict = strict

        self._last_log_at: dict[tuple[str, str, str], float] = {}
        self._log_lock = threading.Lock()

    @property
    def supported_locales(self) -> tuple[str, ...]:
        """已加载语言列表"""
        return tuple(self._catalogs)

    def for_locale(self, locale: str) -> "BoundLocalizer":
        """返回绑定指定语言的 localizer"""
        return BoundLocalizer(self, self._normalize_locale(locale))

    def t(self, key: str, locale: str | None = None, **variables: Any) -> str:
        """翻译普通 key

        fallback 顺序：显式 locale / ContextVar → 默认语言 → key 本身。
        """
        requested = locale or get_current_locale() or self.default_locale
        candidates = [self._normalize_locale(requested)]
        if self.default_locale not in candidates:
            candidates.append(self.default_locale)

        for candidate in candidates:
            template = self._catalogs.get(candidate, {}).get(key)
            if template is None:
                self._handle_problem(
                    kind="missing_key",
                    locale=candidate,
                    key=key,
                    message=f"缺少翻译 key [{candidate}:{key}]",
                    exception_type=KeyError,
                )
                continue

            rendered = self._render(candidate, key, template, variables)
            if rendered is not None:
                return rendered

        return key

    def tp(
        self,
        base_key: str,
        count: int,
        locale: str | None = None,
        **variables: Any,
    ) -> str:
        """翻译复数 key

        - en: count == 1 使用 one，否则 other
        - 中文语言: 始终使用 other
        """
        requested = locale or get_current_locale() or self.default_locale
        normalized = self._normalize_locale(requested)
        category = (
            _PLURAL_CATEGORY_ONE if normalized == "en" and count == 1 else _PLURAL_CATEGORY_OTHER
        )
        variables.setdefault("count", count)
        return self.t(f"{base_key}.{category}", locale=normalized, **variables)

    def _normalize_locale(self, locale: str) -> str:
        normalized = normalize_supported_locale(locale, self._catalogs)
        if normalized is not None:
            return normalized
        message = f"不支持的 locale: {locale}"
        if self.strict:
            raise ValueError(message)
        self._log_limited(
            "unsupported_locale", locale, "", f"{message}，降级到 {self.default_locale}"
        )
        return self.default_locale

    def _render(
        self,
        locale: str,
        key: str,
        template: str,
        variables: Mapping[str, Any],
    ) -> str | None:
        """渲染已校验模板，返回 None 表示渲染失败（非严格模式已记录日志）"""
        placeholders = self._placeholders[locale].get(key, frozenset())
        missing = placeholders - set(variables)
        if missing:
            message = f"缺少模板变量 [{locale}:{key}]: {sorted(missing)}"
            if self.strict:
                raise TranslationError(message)
            self._log_limited("missing_variable", locale, key, message)
            return None
        # 占位符已校验为简单命名，format_map 对多余变量容忍，无注入风险。
        # 兜底 __format__ 等渲染期异常，避免向调用方传播。
        try:
            return template.format_map(dict(variables))
        except Exception as exc:
            message = f"模板渲染失败 [{locale}:{key}]: {type(exc).__name__}: {exc}"
            if self.strict:
                raise TranslationError(message) from exc
            self._log_limited("render_error", locale, key, message)
            return None

    def _handle_problem(
        self,
        kind: str,
        locale: str,
        key: str,
        message: str,
        exception_type: type[Exception],
    ) -> None:
        if self.strict:
            raise exception_type(message)
        self._log_limited(kind, locale, key, message)

    def _log_limited(self, kind: str, locale: str, key: str, message: str) -> None:
        """按 (kind, locale, key) 进程内限频，避免日志洪泛"""
        identity = (kind, locale, key)
        now = time.monotonic()
        with self._log_lock:
            last = self._last_log_at.get(identity, 0.0)
            if now - last < self.LOG_INTERVAL_SECONDS:
                return
            self._last_log_at[identity] = now
        logger.error(message)


@dataclass(frozen=True, slots=True)
class BoundLocalizer:
    """绑定单个 locale 的轻量 localizer"""

    translator: Translator
    locale: str

    def t(self, key: str, **variables: Any) -> str:
        """使用绑定语言翻译普通 key"""
        return self.translator.t(key, locale=self.locale, **variables)

    def tp(self, base_key: str, count: int, **variables: Any) -> str:
        """使用绑定语言翻译复数 key"""
        return self.translator.tp(base_key, count, locale=self.locale, **variables)
