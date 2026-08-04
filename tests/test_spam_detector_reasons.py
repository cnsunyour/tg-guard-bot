"""SpamDetector reasons 编码格式测试（3c13 范围外收尾）。

验证 src/services/spam_detector.py 5 处固定中文 reasons 已 code 化：
- Stage 2 ML → ml_classifier:confidence=...（两位小数）
- Stage 3 Embedding → embedding_similarity:similarity=...
- 全失败兜底 → [ReasonCode.all_detectors_failed]
- 上下文调整 → reply_relevant / topic_consistent（result["reasons"] 存 code）
- reason_logs 双轨：日志保留中文（不直接断言日志，验证 reasons 与日志数据分离）
- 三语真实 catalog 渲染 5 新 code 无 TranslationError
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.i18n.catalog import load_catalogs
from src.core.i18n.translator import Translator
from src.ml.rule_engine import ReasonCode

pytestmark = pytest.mark.unit

CHAT_ID = -100123
USER_ID = 42


def _make_detector(
    *,
    rule_spam: bool = False,
    classifier_trained: bool = True,
    embedder_initialized: bool = True,
) -> tuple["SpamDetector", MagicMock, MagicMock]:  # noqa: F821
    """构造 SpamDetector（mock rule_engine/classifier/embedder/ai_detector）。

    返回 (detector, mock_classifier, mock_embedder)，由调用方配置 predict 等。
    """
    with (
        patch("src.services.spam_detector.get_rule_engine") as mock_get_rule,
        patch("src.services.spam_detector.get_classifier") as mock_get_cls,
        patch("src.services.spam_detector.get_embedder") as mock_get_emb,
        patch("src.services.spam_detector.get_ai_detector") as mock_get_ai,
    ):
        mock_rule = MagicMock()
        mock_rule.analyze = MagicMock(
            return_value={
                "is_spam": rule_spam,
                "confidence": 0.0,
                "reasons": [],
                "details": {},
            }
        )
        mock_get_rule.return_value = mock_rule

        mock_cls = MagicMock()
        mock_cls.is_trained = classifier_trained
        mock_get_cls.return_value = mock_cls

        mock_emb = MagicMock()
        mock_emb.is_initialized = embedder_initialized
        mock_get_emb.return_value = mock_emb

        mock_ai = MagicMock()
        mock_ai.enabled = False
        mock_get_ai.return_value = mock_ai

        from src.services.spam_detector import SpamDetector

        return SpamDetector(), mock_cls, mock_emb


# ===== Stage 2 ML → ml_classifier =====
async def test_detect_stage2_ml_returns_ml_classifier_code() -> None:
    """ML 分类器命中 → reasons == ["ml_classifier:confidence=0.90"]（两位小数）。"""
    detector, mock_cls, _ = _make_detector(
        rule_spam=False, classifier_trained=True, embedder_initialized=False
    )
    mock_cls.predict = MagicMock(return_value=(True, 0.90))

    with (
        patch("src.services.spam_detector.settings.spam_min_text_length", 0),
        patch("src.services.spam_detector.settings.spam_threshold_ml", 0.7),
    ):
        result = await detector.detect("spam text", USER_ID, CHAT_ID)

    assert result["is_spam"]
    assert result["stage"] == "ml_classifier"
    assert result["reasons"] == ["ml_classifier:confidence=0.90"]


# ===== Stage 3 Embedding → embedding_similarity =====
async def test_detect_stage3_embedding_returns_embedding_similarity_code() -> None:
    """Embedding 命中 → reasons == ["embedding_similarity:similarity=0.92"]。"""
    detector, _, mock_emb = _make_detector(
        rule_spam=False, classifier_trained=False, embedder_initialized=True
    )
    mock_emb.predict = MagicMock(return_value=(True, 0.92))

    with patch("src.services.spam_detector.settings.spam_min_text_length", 0):
        result = await detector.detect("spam text", USER_ID, CHAT_ID)

    assert result["is_spam"]
    assert result["stage"] == "embedding"
    assert result["reasons"] == ["embedding_similarity:similarity=0.92"]


# ===== 全失败兜底 → all_detectors_failed =====
async def test_merge_both_failed_returns_all_detectors_failed_code() -> None:
    """传统+AI 都失败 → reasons == [ReasonCode.all_detectors_failed]。"""
    detector, _, _ = _make_detector()
    result = await detector._merge_detection_results(None, None, "text", USER_ID)

    assert result["reasons"] == [ReasonCode.all_detectors_failed]
    # StrEnum 继承 str，序列化后即字符串 code（Redis state 兼容）
    assert result["reasons"] == ["all_detectors_failed"]


# ===== 上下文调整 → reply_relevant / topic_consistent =====
async def test_apply_context_adjustment_appends_reply_relevant_code() -> None:
    """回复链相关 → reasons 含 reply_relevant:similarity=...；原 rule_match code 保留。"""
    detector, _, mock_emb = _make_detector(
        rule_spam=False, classifier_trained=False, embedder_initialized=True
    )
    mock_emb.compute_similarity = AsyncMock(return_value=0.80)  # >= threshold 0.5
    mock_emb.detect_context_consistency = AsyncMock(return_value=(False, 0.0))

    result: dict = {
        "is_spam": True,
        "confidence": 0.9,
        "original_confidence": 0.9,
        "activity_reduction": 0.0,
        "stage": "rule_engine",
        "reasons": ["rule_match:description=test"],
        "details": {},
    }
    message = MagicMock()
    reply_msg = MagicMock()
    reply_msg.text = "相关回复"
    message.reply_to_message = reply_msg

    with (
        patch("src.services.spam_detector.settings.reply_similarity_threshold", 0.5),
        patch("src.services.spam_detector.settings.reply_confidence_reduction", 0.2),
        patch("src.services.spam_detector.settings.context_confidence_reduction", 0.15),
        patch("src.services.spam_detector.settings.spam_threshold_embedding", 0.75),
    ):
        adjusted = await detector._apply_context_adjustment(result, "text", message, [], USER_ID)

    # reply_relevant code 追加到原 reasons 后
    assert "reply_relevant:similarity=0.80" in adjusted["reasons"]
    assert "rule_match:description=test" in adjusted["reasons"]
    # 0.9 - 0.2 = 0.7 < 0.75 → 改判非垃圾
    assert adjusted["is_spam"] is False


async def test_apply_context_adjustment_appends_topic_consistent_code() -> None:
    """群组话题一致 → reasons 含 topic_consistent:similarity=...。"""
    detector, _, mock_emb = _make_detector(
        rule_spam=False, classifier_trained=False, embedder_initialized=True
    )
    mock_emb.compute_similarity = AsyncMock(return_value=0.30)  # < 0.5，不触发 reply
    mock_emb.detect_context_consistency = AsyncMock(return_value=(True, 0.85))

    result: dict = {
        "is_spam": True,
        "confidence": 0.95,
        "original_confidence": 0.95,
        "activity_reduction": 0.0,
        "stage": "rule_engine",
        "reasons": ["rule_match:description=test"],
        "details": {},
    }
    message = MagicMock()
    message.reply_to_message = None  # 无回复链，只测话题一致

    context_messages = [{"text": "话题1"}, {"text": "话题2"}, {"text": "话题3"}]

    with (
        patch("src.services.spam_detector.settings.reply_similarity_threshold", 0.5),
        patch("src.services.spam_detector.settings.reply_confidence_reduction", 0.2),
        patch("src.services.spam_detector.settings.context_confidence_reduction", 0.15),
        patch("src.services.spam_detector.settings.spam_threshold_embedding", 0.75),
    ):
        adjusted = await detector._apply_context_adjustment(
            result, "text", message, context_messages, USER_ID
        )

    assert "topic_consistent:similarity=0.85" in adjusted["reasons"]
    # 0.95 - 0.15 = 0.80 >= 0.75 → 仍判垃圾
    assert adjusted["is_spam"] is True


# ===== 三语真实 catalog 渲染 5 新 code 无 TranslationError =====
@pytest.mark.parametrize("locale", ["zh-Hans", "zh-Hant", "en"])
def test_new_reason_codes_render_real_catalog_without_placeholders(locale: str) -> None:
    """5 新 code 用真实 catalog strict Translator 渲染无残留占位符。"""
    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    localizer = Translator(catalogs, "zh-Hans", strict=True).for_locale(locale)

    from src.bot.handlers.antispam_render import _format_reasons

    text = _format_reasons(
        localizer,
        (
            "ml_classifier:confidence=0.85",
            "embedding_similarity:similarity=0.92",
            "all_detectors_failed",
            "reply_relevant:similarity=0.72",
            "topic_consistent:similarity=0.85",
        ),
    )
    # 严格模式 + 真实 catalog：不应残留任何 { } 占位符
    assert "{" not in text
    assert "}" not in text
