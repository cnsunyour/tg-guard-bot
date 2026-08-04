"""P2.4 CAS/status 群封禁通知 i18n 测试。

覆盖：
- catalog 7 key 三语存在（cas_ban.notify + status_ban.notify + 5 status label）
- CAS/status 通知用真实 catalog strict 渲染无残留占位符
- status_label_key_map 映射逻辑（reason → key，未知 → unknown.label）契约
"""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REASON_LABEL_KEYS = {
    "restricted": "verification.join.status_ban.restricted.label",
    "scam": "verification.join.status_ban.scam.label",
    "fake": "verification.join.status_ban.fake.label",
    "deleted": "verification.join.status_ban.deleted.label",
}
_ALL_LABEL_KEYS = [*_REASON_LABEL_KEYS.values(), "verification.join.status_ban.unknown.label"]


def test_cas_status_ban_keys_exist_in_three_locales() -> None:
    """7 key 三语均存在。"""
    root = Path(__file__).resolve().parents[1]
    expected = {
        "verification.join.cas_ban.notify",
        "verification.join.status_ban.notify",
        *_ALL_LABEL_KEYS,
    }
    for locale in ("zh-Hans", "zh-Hant", "en"):
        catalog = json.loads((root / "locales" / f"{locale}.json").read_text("utf-8"))
        missing = expected - set(catalog)
        assert not missing, f"{locale} 缺: {missing}"


@pytest.mark.parametrize("locale", ["zh-Hans", "zh-Hant", "en"])
def test_cas_ban_notify_renders_without_placeholders(locale: str) -> None:
    """CAS 封禁通知用真实 catalog strict 渲染无残留占位符。"""
    from src.core.i18n.catalog import load_catalogs
    from src.core.i18n.translator import Translator

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    localizer = Translator(catalogs, "zh-Hans", strict=True).for_locale(locale)

    text = localizer.t("verification.join.cas_ban.notify", user="用户名")
    assert "{" not in text
    assert "}" not in text
    assert "用户名" in text


@pytest.mark.parametrize("locale", ["zh-Hans", "zh-Hant", "en"])
def test_status_ban_notify_renders_each_reason_without_placeholders(locale: str) -> None:
    """4 个已知 reason + unknown 用真实 catalog strict 渲染 status_ban.notify 无残留占位符。"""
    from src.core.i18n.catalog import load_catalogs
    from src.core.i18n.translator import Translator

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    localizer = Translator(catalogs, "zh-Hans", strict=True).for_locale(locale)

    for label_key in _ALL_LABEL_KEYS:
        status_text = localizer.t(label_key)
        notify = localizer.t(
            "verification.join.status_ban.notify",
            user="用户名",
            status=status_text,
        )
        assert "{" not in notify
        assert "}" not in notify


def test_status_label_key_map_covers_all_known_reasons() -> None:
    """_process_user_join 的 status_label_key_map 契约：4 reason 映射 + 未知兜底 unknown。

    这里只锁 key 名集合（映射逻辑在 handler 内，靠 catalog key 存在性保证渲染成功）。
    """
    # 已知 4 reason 必须有对应 label key
    expected_reason_keys = set(_REASON_LABEL_KEYS.values())
    root = Path(__file__).resolve().parents[1]
    for locale in ("zh-Hans", "zh-Hant", "en"):
        catalog = json.loads((root / "locales" / f"{locale}.json").read_text("utf-8"))
        assert expected_reason_keys <= set(catalog)
    # unknown 兜底 key 必须存在（None/脏值/未来新增状态走它）
    assert all(
        "verification.join.status_ban.unknown.label"
        in json.loads((root / "locales" / f"{loc}.json").read_text("utf-8"))
        for loc in ("zh-Hans", "zh-Hant", "en")
    )
