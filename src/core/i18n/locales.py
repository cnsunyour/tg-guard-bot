"""BCP 47 locale 地区别名归一化。

catalog 只维护规范 locale（zh-Hans/zh-Hant/en），但外部来源（DB 历史值、Telegram
``language_code``）可能用地区变体（zh-CN/zh-TW/en-US 等）。本模块把这些变体归一到规范
catalog 名，供 translator / resolver / /lang 三入口共用。

**设计选择：宽松归一，非 BCP 47 解析器。**

- 已知地区精确别名表（zh-CN/zh-SG → zh-Hans，zh-TW/zh-HK/zh-MO → zh-Hant）；
- ``en-*`` 通配 → en（Telegram ``language_code`` 的英语地区变体共用通用英文 catalog）；
- ``zh-Hans-*``/``zh-Hant-*`` 的显式 script（subtags[1]）优先于地区；
- ``isascii`` 拒绝 Unicode 畸形（如 ``en-中文``）；
- 不兜底未知 zh-* 变体（如 ``zh-Foo``），避免把脏数据/笔误静默当简体。

不校验 subtag 语义结构（如 ``en-US-12`` 的非合法后续）——本模块面向 Telegram
``language_code`` 与 /lang 写入（实践来源均合法），畸形值容忍映射不崩溃，用户可 /lang
修正。完整 BCP 47 校验需 langcodes 等库，超出本模块范围。
"""

from collections.abc import Collection
from typing import Final

# lookup 前统一 casefold，键用小写。
_LOCALE_ALIASES: Final[dict[str, str]] = {
    # 中文简体地区（中国大陆、新加坡）+ 无地区默认简体
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    # 中文繁体地区（台湾、香港、澳门）
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
}


def _resolve_locale_alias(locale: str) -> str:
    """返回已知 locale 的 catalog 别名；未知值原样返回（交调用方处理）。"""
    folded = locale.casefold()
    direct = _LOCALE_ALIASES.get(folded)
    if direct is not None:
        return direct

    subtags = folded.split("-")
    # isascii 拒绝 Unicode（如 中文），防畸形值被静默映射。
    if any(not subtag or not subtag.isascii() or not subtag.isalnum() for subtag in subtags):
        return locale

    language = subtags[0]
    if language == "en":
        # 通用英文 catalog：所有 ASCII en-* 变体共用（宽松归一，非 BCP 47 解析器）。
        return "en"
    if language == "zh" and len(subtags) > 1 and subtags[1] in ("hans", "hant"):
        # 显式 script（subtags[1]）优先于地区：zh-Hant-CN 仍繁体。
        # 只看 script 位置，避免私有用段（x-hant）或后续段误判。
        return "zh-Hans" if subtags[1] == "hans" else "zh-Hant"
    return locale


def normalize_supported_locale(
    locale: str,
    supported_locales: Collection[str],
) -> str | None:
    """将 locale 归一到实际支持的 catalog 名；无法归一返回 ``None``。

    匹配顺序：精确 → 大小写无关 → 已知地区别名/宽松通配。
    """
    if locale in supported_locales:
        return locale

    folded = locale.casefold()
    for supported in supported_locales:
        if supported.casefold() == folded:
            return supported

    aliased = _resolve_locale_alias(locale)
    return aliased if aliased in supported_locales else None
