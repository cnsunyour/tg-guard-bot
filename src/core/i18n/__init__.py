"""i18n 多语言基础设施"""

import os
from pathlib import Path

from loguru import logger

from src.core.config import settings
from src.core.i18n.catalog import load_catalogs
from src.core.i18n.resolver import LocaleResolver
from src.core.i18n.translator import BoundLocalizer, Translator

_translator: Translator | None = None
_resolver: LocaleResolver | None = None


def init_i18n(catalog_dir: Path | None = None, force: bool = False) -> Translator:
    """加载 catalog 并初始化全局 Translator 与 LocaleResolver

    默认目录为项目根目录下的 ``locales/``。源语言缺失、损坏或 catalog
    校验失败时异常向上抛出，阻止 Bot 以不完整的源语言启动。
    """
    global _translator, _resolver
    if _translator is not None and not force:
        return _translator

    resolved_dir = catalog_dir if catalog_dir is not None else _default_catalog_dir()

    catalogs = load_catalogs(
        catalog_dir=resolved_dir,
        locales=settings.supported_locales,
        default_locale=settings.default_locale,
    )

    strict = _resolve_strict()
    _translator = Translator(
        catalogs=catalogs,
        default_locale=settings.default_locale,
        strict=strict,
    )
    _resolver = LocaleResolver()
    logger.info(
        f"i18n catalog 已加载 [语言:{','.join(_translator.supported_locales)}] "
        f"[严格模式:{strict}]"
    )
    return _translator


def get_translator() -> Translator:
    """获取已初始化的全局 Translator"""
    if _translator is None:
        raise RuntimeError("i18n 尚未初始化，请先调用 init_i18n()")
    return _translator


def get_resolver() -> LocaleResolver:
    """获取已初始化的全局 LocaleResolver

    供非 handler 路径（验证流程的普通函数、定时任务等）按目的地解析 locale，
    避免在调用链里逐层传递 resolver。
    """
    if _resolver is None:
        raise RuntimeError("i18n 尚未初始化，请先调用 init_i18n()")
    return _resolver


def _default_catalog_dir() -> Path:
    # 本文件位于 src/core/i18n/__init__.py，上溯 3 级到项目根
    return Path(__file__).resolve().parents[3] / "locales"


def _resolve_strict() -> bool:
    env = os.getenv("I18N_STRICT")
    if env is None:
        return settings.debug
    return env.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "BoundLocalizer",
    "LocaleResolver",
    "Translator",
    "get_resolver",
    "get_translator",
    "init_i18n",
]
