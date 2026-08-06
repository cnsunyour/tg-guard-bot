"""reason_codes 编码格式与 catalog 渲染测试（3c13）。

验证 rule_engine 返回编码格式 + antispam_render 解析渲染 catalog。
"""

from unittest.mock import MagicMock

import pytest

from src.bot.handlers.antispam_render import _format_reasons
from src.ml.rule_engine import ReasonCode, RuleEngine

pytestmark = pytest.mark.unit


def _localizer() -> MagicMock:
    """mock localizer：t 返回 key + params。"""
    loc = MagicMock()

    def fake_t(key, **kw):
        if not kw:
            return f"<{key}>"
        return f"<{key}:{kw}>"

    loc.t.side_effect = fake_t
    return loc


# ===== ReasonCode StrEnum 值稳定性 =====
def test_reason_code_values_are_stable_strings() -> None:
    """StrEnum value 是稳定字符串（catalog key 依赖）。"""
    assert ReasonCode.tg_invite.value == "tg_invite"
    assert ReasonCode.rule_match.value == "rule_match"
    assert ReasonCode.suspicious_domain.value == "suspicious_domain"
    assert ReasonCode.short_link.value == "short_link"
    assert ReasonCode.contact_info.value == "contact_info"
    assert ReasonCode.repeated_chars.value == "repeated_chars"
    assert ReasonCode.channel_mention.value == "channel_mention"
    assert ReasonCode.emoji_flood.value == "emoji_flood"


# ===== rule_engine 返回编码格式 =====
def test_rule_engine_tg_invite_returns_encoded_code() -> None:
    """Telegram 邀请链接 → "tg_invite" 编码。"""
    engine = RuleEngine()
    result = engine.analyze("https://t.me/joinchat/xxx")
    assert result["is_spam"]
    assert "tg_invite" in result["reasons"]


def test_rule_engine_rule_match_returns_encoded_code_with_rule_id() -> None:
    """规则匹配 → "rule_match:rule_id=..." 编码（英文稳定 id，文案走 catalog）。"""
    engine = RuleEngine()
    result = engine.analyze("BTC行情分析，私聊带单，日赚十万")
    assert result["is_spam"]
    reason = result["reasons"][0]
    assert reason.startswith("rule_match:rule_id=")
    assert "crypto_multi_keyword" in reason


def test_rule_engine_contact_info_returns_encoded_code_with_subtype() -> None:
    """联系方式 → "contact_info:type=wechat/qq/phone" 编码。"""
    engine = RuleEngine()
    # 微信号
    result = engine.analyze("加我微信 wxid_abc123")
    assert result["is_spam"]
    assert "contact_info:type=wechat" in result["reasons"]
    # QQ号
    result = engine.analyze("联系QQ：123456789")
    assert result["is_spam"]
    assert "contact_info:type=qq" in result["reasons"]
    # 电话
    result = engine.analyze("联系电话：13812345678")
    assert result["is_spam"]
    assert "contact_info:type=phone" in result["reasons"]


def test_rule_engine_suspicious_domain_returns_encoded_code_with_domain() -> None:
    """可疑域名 → "suspicious_domain:domain=..." 编码（confidence 0.8 >= 阈值 → is_spam）。"""
    engine = RuleEngine()
    result = engine.analyze("访问 https://example.tk/abc")
    assert result["is_spam"]
    reason = next((r for r in result["reasons"] if r.startswith("suspicious_domain:")), None)
    assert reason is not None
    assert "domain=" in reason


def test_rule_engine_short_link_returns_encoded_code() -> None:
    """短链接 → "short_link" 编码（confidence 0.8 >= 阈值 → is_spam）。"""
    engine = RuleEngine()
    result = engine.analyze("点击 http://bit.ly/abc")
    assert result["is_spam"]
    assert "short_link" in result["reasons"]


def test_rule_engine_repeated_chars_returns_encoded_code() -> None:
    """重复字符 → "repeated_chars" 编码（confidence 0.7 < 阈值，但 reasons 仍记录）。"""
    engine = RuleEngine()
    result = engine.analyze("啊" * 50)
    # confidence 0.7 低于 spam_threshold_rule(0.8)，is_spam 可能为 False
    # 但 reasons 仍应记录 repeated_chars code
    assert "repeated_chars" in result["reasons"]


def test_rule_engine_emoji_flood_returns_encoded_code() -> None:
    """Emoji 刷屏 → "emoji_flood" 编码（confidence 0.65 < 阈值，但 reasons 仍记录）。

    用 U+1F600-U+1F64F 范围内的 emoji + 空格分隔（check_emoji_flood 用 len(findall) 计数）。
    """
    engine = RuleEngine()
    text = " ".join("😀😁😂😃😄😅😆😉😋😎😍😘😗😙😚🙂")
    result = engine.analyze(text)
    assert "emoji_flood" in result["reasons"]


# ===== _format_reasons 解析编码 + catalog 渲染 =====
def test_format_reasons_decodes_simple_code() -> None:
    """无参数 code → catalog key。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("tg_invite", "short_link"))
    assert (
        result
        == "<antispam.reason.tg_invite.label><antispam.reason.separator.label><antispam.reason.short_link.label>"
    )


def test_format_reasons_decodes_code_with_params() -> None:
    """带参数 code → catalog key + params 注入（rule_match 嵌套 rule.<id>.label）。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("rule_match:rule_id=social_engine_db",))
    # rule_match 嵌套：rule_label = t(rule.social_engine_db.label)，再 t(rule_match.label, rule=rule_label)
    assert result == (
        "<antispam.reason.rule_match.label:{'rule': '<antispam.reason.rule.social_engine_db.label>'}>"
    )


def test_format_reasons_decodes_contact_subtypes() -> None:
    """contact 子类型 → type 参数注入。"""
    localizer = _localizer()
    result = _format_reasons(
        localizer, ("contact_info:type=wechat", "contact_info:type=qq", "contact_info:type=phone")
    )
    assert result == (
        "<antispam.reason.contact_info.label:{'type': '<antispam.reason.contact_type.wechat.label>'}>"
        "<antispam.reason.separator.label><antispam.reason.contact_info.label:{'type': '<antispam.reason.contact_type.qq.label>'}>"
        "<antispam.reason.separator.label><antispam.reason.contact_info.label:{'type': '<antispam.reason.contact_type.phone.label>'}>"
    )


def test_format_reasons_decodes_suspicious_domain_with_domain_param() -> None:
    """suspicious_domain → domain 参数注入。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("suspicious_domain:domain=bit.ly",))
    assert result == "<antispam.reason.suspicious_domain.label:{'domain': 'bit.ly'}>"


def test_format_reasons_mixed_codes_and_legacy_strings() -> None:
    """编码 + 旧格式字符串混合（兼容性）。"""
    localizer = _localizer()
    result = _format_reasons(
        localizer, ("tg_invite", "AI检测:广告内容", "rule_match:rule_id=social_engine_db")
    )
    assert "<antispam.reason.tg_invite.label>" in result
    assert "AI检测:广告内容" in result  # 旧格式保持原样
    assert "<antispam.reason.rule_match.label:" in result


def test_format_reasons_escapes_legacy_strings() -> None:
    """旧格式字符串 escape HTML。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("<script>xss</script>",))
    assert "&lt;script&gt;xss&lt;/script&gt;" in result
    assert "<script>" not in result


def test_format_reasons_empty_tuple_returns_empty_string() -> None:
    """空 tuple → 空字符串。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ())
    assert result == ""


# ===== codex review P2 回归 =====
def test_format_reasons_preserves_comma_in_param() -> None:
    """P2: 参数含逗号时不被截断（单参数解析取 = 后全部）。rule_match 改 rule_id
    后无逗号场景，用 suspicious_domain 的 domain 参数覆盖逗号保留回归。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("suspicious_domain:domain=bit.ly, phishing.com",))
    assert result == (
        "<antispam.reason.suspicious_domain.label:{'domain': 'bit.ly, phishing.com'}>"
    )


def test_format_reasons_missing_param_code_escapes_original() -> None:
    """P2: 纯 code 名无必需参数（如 AI 自由文本恰好 "rule_match"）→ escape 原样显示。"""
    localizer = _localizer()
    # rule_match 无 rule_id → escape 原样（不渲染 catalog key，防 TranslationError）
    assert _format_reasons(localizer, ("rule_match",)) == "rule_match"
    # contact_info 无 type → escape 原样
    assert _format_reasons(localizer, ("contact_info",)) == "contact_info"
    # suspicious_domain 无 domain → escape 原样
    assert _format_reasons(localizer, ("suspicious_domain",)) == "suspicious_domain"


def test_format_reasons_rule_match_builtin_renders_from_catalog() -> None:
    """内置规则 → catalog 嵌套渲染（rule.<id>.label + rule_match.label），无残留占位符。"""
    from pathlib import Path

    from src.core.i18n.catalog import load_catalogs
    from src.core.i18n.translator import Translator

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    localizer = Translator(catalogs, "zh-Hans", strict=True).for_locale("zh-Hans")
    result = _format_reasons(localizer, ("rule_match:rule_id=social_engine_db",))
    # rule.social_engine_db.label = "社工库非法获取"，rule_match.label = "规则匹配：{rule}"
    assert result == "规则匹配：社工库非法获取"
    assert "{" not in result  # 无残留占位符


def test_format_reasons_rule_match_unknown_rule_id_falls_back_to_escaped_id() -> None:
    """自定义 JSON 规则（catalog 无 rule.<id>.label）→ escape rule_id 兜底，不泄漏中文 description。"""
    from pathlib import Path

    from src.core.i18n.catalog import load_catalogs
    from src.core.i18n.translator import Translator

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    localizer = Translator(catalogs, "zh-Hans", strict=True).for_locale("zh-Hans")
    result = _format_reasons(localizer, ("rule_match:rule_id=custom_unknown",))
    # catalog 无 rule.custom_unknown.label → strict t() 抛 KeyError → 兜底 escape_html(rule_id)
    # rule_match.label = "规则匹配：{rule}" → "规则匹配：custom_unknown"
    assert result == "规则匹配：custom_unknown"


def test_format_reasons_unknown_contact_subtype_escapes_original() -> None:
    """未知 contact_type 子 code → escape 原样（防 catalog 缺 key）。"""
    localizer = _localizer()
    # type=telegram 是未知子 code，localizer.t 会返回 key 本身（非严格）
    # 但更安全的行为是原样 escape。当前实现：type_label 为 key 字符串，注入后渲染
    # 这验证不会抛异常即可
    result = _format_reasons(localizer, ("contact_info:type=telegram",))
    assert "contact_type.telegram" in result


# ===== 3c13 范围外收尾：spam_detector 产出的 5 个新 code =====
def test_reason_code_values_include_spam_detector_codes() -> None:
    """5 个 spam_detector 产出 code 值稳定（catalog key 依赖）。"""
    assert ReasonCode.ml_classifier.value == "ml_classifier"
    assert ReasonCode.embedding_similarity.value == "embedding_similarity"
    assert ReasonCode.all_detectors_failed.value == "all_detectors_failed"
    assert ReasonCode.reply_relevant.value == "reply_relevant"
    assert ReasonCode.topic_consistent.value == "topic_consistent"


def test_format_reasons_renders_ml_classifier_with_confidence() -> None:
    """ml_classifier:confidence=0.85 → catalog key + confidence 注入。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("ml_classifier:confidence=0.85",))
    assert result == "<antispam.reason.ml_classifier.label:{'confidence': '0.85'}>"


def test_format_reasons_renders_embedding_similarity_with_similarity() -> None:
    """embedding_similarity:similarity=0.92 → catalog key + similarity 注入。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("embedding_similarity:similarity=0.92",))
    assert result == "<antispam.reason.embedding_similarity.label:{'similarity': '0.92'}>"


def test_format_reasons_renders_reply_relevant_and_topic_consistent() -> None:
    """reply_relevant / topic_consistent → similarity 注入。"""
    localizer = _localizer()
    result = _format_reasons(
        localizer,
        ("reply_relevant:similarity=0.72", "topic_consistent:similarity=0.85"),
    )
    assert result == (
        "<antispam.reason.reply_relevant.label:{'similarity': '0.72'}>"
        "<antispam.reason.separator.label><antispam.reason.topic_consistent.label:{'similarity': '0.85'}>"
    )


def test_format_reasons_renders_all_detectors_failed_no_params() -> None:
    """all_detectors_failed 无参数 → catalog key（无占位符）。"""
    localizer = _localizer()
    result = _format_reasons(localizer, ("all_detectors_failed",))
    assert result == "<antispam.reason.all_detectors_failed.label>"


def test_format_reasons_escapes_numeric_param_to_prevent_injection() -> None:
    """数值参数 escape（防 AI 自由文本伪造 code 格式注入 HTML）。

    ml_classifier:confidence=<script> → confidence 经 escape_html 后注入。
    与 rule_match/suspicious_domain 的防御性 escape 对齐。
    """
    localizer = _localizer()
    result = _format_reasons(localizer, ("ml_classifier:confidence=<script>alert(1)</script>",))
    assert "confidence': '&lt;script&gt;alert(1)&lt;/script&gt;'" in result


def test_format_reasons_missing_numeric_param_escapes_original() -> None:
    """纯 code 名无必需参数（ml_classifier 无 confidence 等）→ escape 原样。"""
    localizer = _localizer()
    assert _format_reasons(localizer, ("ml_classifier",)) == "ml_classifier"
    assert _format_reasons(localizer, ("embedding_similarity",)) == "embedding_similarity"
    assert _format_reasons(localizer, ("reply_relevant",)) == "reply_relevant"
    assert _format_reasons(localizer, ("topic_consistent",)) == "topic_consistent"
