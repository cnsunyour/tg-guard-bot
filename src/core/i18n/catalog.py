"""JSON 翻译目录（catalog）的加载与校验

catalog 采用扁平点分 key（如 ``verification.join.group.welcome``），
存放于 ``locales/{locale}.json``。本模块负责：

- 加载时检测重复 key（Python ``json`` 默认会静默覆盖）；
- 校验 key 命名、占位符（仅允许简单命名占位）、Telegram HTML 子集；
- 跨语言 key/占位符一致性（parity）校验。

源语言（zh-Hans）缺失或损坏将抛出异常以阻止 Bot 启动。
"""

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from string import Formatter
from typing import Any

# key 形如 module.flow.state.surface，全小写、点分、至少两段
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
# 占位符仅允许简单标识符 {name}，禁止 {obj.attr} / {x[0]} / {v!r} / {n:02d}
PLACEHOLDER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Telegram 支持的 HTML 标签白名单
# 参考: https://core.telegram.org/bots/api#html-style
ALLOWED_HTML_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "code",
        "del",
        "em",
        "i",
        "ins",
        "pre",
        "s",
        "span",
        "strike",
        "strong",
        "tg-spoiler",
        "u",
    }
)

ALLOWED_HTML_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href"}),
    "blockquote": frozenset({"expandable"}),
    "code": frozenset({"class"}),
    "pre": frozenset({"language"}),
    "span": frozenset({"class"}),
    "tg-spoiler": frozenset(),
}


class CatalogError(ValueError):
    """翻译目录错误"""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json object_pairs_hook：阻止重复 key 被静默覆盖"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"翻译目录存在重复 key: {key}")
        result[key] = value
    return result


def extract_placeholders(template: str) -> frozenset[str]:
    """提取并校验模板中的简单命名占位符

    允许 ``{name}``、``{count}``；禁止属性访问、下标、conversion、format spec。
    """
    placeholders: set[str] = set()
    formatter = Formatter()
    try:
        for _literal, field_name, format_spec, conversion in formatter.parse(template):
            if field_name is None:
                continue
            if not PLACEHOLDER_NAME_PATTERN.fullmatch(field_name):
                raise CatalogError(f"只允许简单命名占位符: {{{field_name}}}")
            if conversion:
                raise CatalogError(f"占位符不允许 conversion: {{{field_name}!{conversion}}}")
            if format_spec:
                raise CatalogError(f"占位符不允许 format spec: {{{field_name}:{format_spec}}}")
            placeholders.add(field_name)
    except ValueError as exc:
        raise CatalogError(f"模板格式错误: {template!r}: {exc}") from exc
    return frozenset(placeholders)


class _TelegramHTMLValidator(HTMLParser):
    """Telegram HTML 子集校验器

    convert_charrefs=False 以保留原始实体，便于在文案层发现非法写法。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_HTML_TAGS:
            raise CatalogError(f"不支持的 HTML 标签: <{tag}>")
        allowed_attrs = ALLOWED_HTML_ATTRIBUTES.get(tag, frozenset())
        for attr_name, attr_value in attrs:
            if attr_name not in allowed_attrs:
                raise CatalogError(f"<{tag}> 不允许属性 {attr_name}")
            if tag == "span" and attr_value != "tg-spoiler":
                raise CatalogError('<span> 仅允许 class="tg-spoiler"')
            if (
                tag == "code"
                and attr_name == "class"
                and not (attr_value or "").startswith("language-")
            ):
                raise CatalogError("<code> 的 class 必须以 language- 开头")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            raise CatalogError(f"出现没有开始标签的 </{tag}>")
        expected = self.stack.pop()
        if expected != tag:
            raise CatalogError(f"HTML 标签嵌套错误，期望 </{expected}>，实际 </{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise CatalogError(f"不支持自闭合 HTML 标签: <{tag}/>")

    def handle_comment(self, data: str) -> None:
        raise CatalogError("翻译文案中不允许 HTML 注释")

    def handle_decl(self, decl: str) -> None:
        raise CatalogError("翻译文案中不允许 HTML 声明")

    def validate_complete(self) -> None:
        if self.stack:
            raise CatalogError(f"HTML 标签未闭合: {', '.join(self.stack)}")


def validate_telegram_html(template: str) -> None:
    """校验字符串是否符合 Telegram HTML 子集"""
    parser = _TelegramHTMLValidator()
    try:
        parser.feed(template)
        parser.close()
        parser.validate_complete()
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError(f"HTML 解析失败: {template!r}: {exc}") from exc


def load_catalog(path: Path) -> dict[str, str]:
    """加载并校验单个 JSON catalog"""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError, CatalogError) as exc:
        raise CatalogError(f"无法加载翻译目录 {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise CatalogError(f"翻译目录根节点必须是对象: {path}")

    catalog: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise CatalogError(f"{path}: 非法翻译 key: {key!r}")
        if not isinstance(value, str):
            raise CatalogError(f"{path}: {key} 的值必须是字符串")
        extract_placeholders(value)
        validate_telegram_html(value)
        catalog[key] = value
    return catalog


def validate_catalog_parity(
    catalogs: dict[str, dict[str, str]],
    default_locale: str,
) -> None:
    """校验所有语言的 key 与占位符与源语言完全一致"""
    source = catalogs.get(default_locale)
    if source is None:
        raise CatalogError(f"缺少源语言 catalog: {default_locale}")

    source_keys = set(source)
    for locale, catalog in catalogs.items():
        keys = set(catalog)
        missing = source_keys - keys
        extra = keys - source_keys
        if missing or extra:
            raise CatalogError(
                f"{locale} 与 {default_locale} key 不一致，"
                f"缺失={sorted(missing)}，多余={sorted(extra)}"
            )
        for key in source_keys:
            if extract_placeholders(source[key]) != extract_placeholders(catalog[key]):
                raise CatalogError(f"{locale}:{key} 占位符与源语言不一致")


def load_catalogs(
    catalog_dir: Path,
    locales: list[str],
    default_locale: str,
) -> dict[str, dict[str, str]]:
    """加载全部支持语言并执行交叉校验

    源语言优先加载，其损坏会立即阻止启动。
    """
    if default_locale not in locales:
        raise CatalogError("default_locale 必须包含在 supported_locales 中")

    ordered = [default_locale, *(loc for loc in locales if loc != default_locale)]
    catalogs: dict[str, dict[str, str]] = {}
    for locale in ordered:
        path = catalog_dir / f"{locale}.json"
        if not path.is_file():
            raise CatalogError(f"缺少翻译目录文件: {path}")
        catalogs[locale] = load_catalog(path)

    validate_catalog_parity(catalogs, default_locale)
    return catalogs
