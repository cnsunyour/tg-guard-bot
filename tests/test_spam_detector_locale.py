"""SpamDetector locale 透传测试（3c14）。

验证 detect_with_ai / detect_with_ai_context / detect_image 用 chat_id 解析 locale
（get_resolver().for_group）并透传给 ai_detector。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

CHAT_ID = -100123
USER_ID = 42


def _make_detector_with_ai():
    """构造 SpamDetector（mock 依赖），返回 (detector, mock_ai)。

    AI 检测启用，规则引擎/分类器/embedder 均不命中（聚焦 AI 路径的 locale 透传）。
    """
    with (
        patch("src.services.spam_detector.get_rule_engine") as mock_get_rule_engine,
        patch("src.services.spam_detector.get_classifier") as mock_get_classifier,
        patch("src.services.spam_detector.get_embedder") as mock_get_embedder,
        patch("src.services.spam_detector.get_ai_detector") as mock_get_ai_detector,
    ):
        mock_rule_engine = MagicMock()
        mock_rule_engine.analyze = MagicMock(
            return_value={"is_spam": False, "confidence": 0.0, "reasons": [], "details": {}}
        )
        mock_get_rule_engine.return_value = mock_rule_engine

        mock_classifier = MagicMock()
        mock_classifier.is_trained = False
        mock_get_classifier.return_value = mock_classifier

        mock_embedder = MagicMock()
        mock_embedder.is_initialized = False
        mock_get_embedder.return_value = mock_embedder

        mock_ai = MagicMock()
        mock_ai.enabled = True  # AI 启用
        mock_ai.detect = AsyncMock(
            return_value={
                "is_spam": False,
                "confidence": 0.1,
                "reasons": [],
                "details": {},
                "stage": "ai_api",
            }
        )
        mock_ai.detect_with_context = AsyncMock(
            return_value={
                "is_spam": False,
                "confidence": 0.1,
                "reasons": [],
                "details": {},
                "stage": "ai_api",
            }
        )
        mock_get_ai_detector.return_value = mock_ai

        from src.services.spam_detector import SpamDetector

        detector = SpamDetector()
        return detector, mock_ai


# ===== detect_with_ai 透传 locale =====
async def test_detect_with_ai_resolves_locale_and_passes_to_ai_detector() -> None:
    """detect_with_ai 用 chat_id 解析 locale → ai_detector.detect(locale=...) 透传。"""
    detector, mock_ai = _make_detector_with_ai()
    with patch("src.services.spam_detector.get_resolver") as mock_get_resolver:
        mock_resolver = MagicMock()
        mock_resolver.for_group = AsyncMock(return_value="en")
        mock_get_resolver.return_value = mock_resolver
        # 短文本预过滤跳过：确保走 AI 路径
        with patch("src.services.spam_detector.settings.spam_min_text_length", 0):
            await detector.detect_with_ai("spam ad content here", USER_ID, CHAT_ID)

    mock_resolver.for_group.assert_awaited_once_with(CHAT_ID)
    mock_ai.detect.assert_awaited_once()
    assert mock_ai.detect.await_args.kwargs.get("locale") == "en"


async def test_detect_with_ai_context_resolves_locale_and_passes() -> None:
    """detect_with_ai_context 用 chat_id 解析 locale → ai_detector.detect_with_context 透传。"""
    detector, mock_ai = _make_detector_with_ai()
    with patch("src.services.spam_detector.get_resolver") as mock_get_resolver:
        mock_resolver = MagicMock()
        mock_resolver.for_group = AsyncMock(return_value="zh-Hant")
        mock_get_resolver.return_value = mock_resolver
        with patch("src.services.spam_detector.settings.spam_min_text_length", 0):
            await detector.detect_with_ai_context(
                "spam ad content", USER_ID, CHAT_ID, context_text="ctx"
            )

    mock_resolver.for_group.assert_awaited_once_with(CHAT_ID)
    mock_ai.detect_with_context.assert_awaited_once()
    assert mock_ai.detect_with_context.await_args.kwargs.get("locale") == "zh-Hant"


async def test_detect_with_ai_context_no_context_still_resolves_locale() -> None:
    """context_text=None 时仍解析 locale（AI 检测内部降级处理）。"""
    detector, mock_ai = _make_detector_with_ai()
    with patch("src.services.spam_detector.get_resolver") as mock_get_resolver:
        mock_resolver = MagicMock()
        mock_resolver.for_group = AsyncMock(return_value="en")
        mock_get_resolver.return_value = mock_resolver
        with patch("src.services.spam_detector.settings.spam_min_text_length", 0):
            await detector.detect_with_ai_context("spam ad", USER_ID, CHAT_ID)

    # 即使无 context，locale 仍被解析并透传给 detect_with_context
    mock_resolver.for_group.assert_awaited_once_with(CHAT_ID)
    assert mock_ai.detect_with_context.await_args.kwargs.get("locale") == "en"
