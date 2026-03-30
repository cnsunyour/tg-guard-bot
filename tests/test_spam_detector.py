"""测试文本长度预过滤功能"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_detector():
    """创建一个 Mock 的 SpamDetector"""
    with (
        patch("src.services.spam_detector.get_rule_engine") as mock_get_rule_engine,
        patch("src.services.spam_detector.get_classifier") as mock_get_classifier,
        patch("src.services.spam_detector.get_embedder") as mock_get_embedder,
        patch("src.services.spam_detector.get_ocr_extractor") as mock_get_ocr_extractor,
        patch("src.services.spam_detector.get_ai_detector") as mock_get_ai_detector,
    ):

        # Mock 规则引擎
        mock_rule_engine = MagicMock()
        mock_get_rule_engine.return_value = mock_rule_engine

        # Mock 分类器
        mock_classifier = MagicMock()
        mock_classifier.is_trained = False
        mock_get_classifier.return_value = mock_classifier

        # Mock embedder
        mock_embedder = MagicMock()
        mock_embedder.is_initialized = False
        mock_get_embedder.return_value = mock_embedder

        # Mock OCR
        mock_ocr = MagicMock()
        mock_ocr.is_available = False
        mock_get_ocr_extractor.return_value = mock_ocr

        # Mock AI detector
        mock_ai = MagicMock()
        mock_ai.enabled = False
        mock_get_ai_detector.return_value = mock_ai

        from src.services.spam_detector import SpamDetector

        detector = SpamDetector()
        yield detector, mock_rule_engine


@pytest.mark.asyncio
async def test_short_message_skip(mock_detector):
    """测试短消息跳过检测"""
    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎返回非垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }
    )

    # 短消息（正常）
    result = await detector.detect("好的", 123, 456)
    assert not result["is_spam"]
    assert result["stage"] == "skipped_short"
    assert result["details"]["normalized_length"] == 2
    assert result["details"]["min_length"] == 10


@pytest.mark.asyncio
async def test_short_spam_detected(mock_detector):
    """测试短垃圾消息仍然被检测"""
    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎检测到垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": True,
            "confidence": 0.95,
            "reasons": ["联系方式"],
            "details": {},
        }
    )

    # 短垃圾消息（包含关键词）
    result = await detector.detect("加微xxx", 123, 456)
    # 规则引擎应该检测到，不会跳过
    assert result["stage"] != "skipped_short"
    assert result["is_spam"]
    assert result["stage"] == "rule_engine"


@pytest.mark.asyncio
async def test_long_message_checked(mock_detector):
    """测试长消息继续检测"""
    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎返回非垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }
    )

    # 长消息（正常）
    result = await detector.detect("这是一条超过十个字符的测试消息", 123, 456)
    # 不应该跳过
    assert result["stage"] != "skipped_short"
    assert not result["is_spam"]


@pytest.mark.asyncio
async def test_threshold_zero(mock_detector):
    """测试阈值为0时禁用功能"""
    from src.core.config import settings

    # 临时修改配置
    original = settings.spam_min_text_length
    settings.spam_min_text_length = 0

    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎返回非垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }
    )

    result = await detector.detect("好", 123, 456)
    # 不应该跳过
    assert result["stage"] != "skipped_short"

    # 恢复配置
    settings.spam_min_text_length = original


@pytest.mark.asyncio
async def test_empty_message_skip(mock_detector):
    """测试空消息跳过检测"""
    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎返回非垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }
    )

    # 空字符串
    result = await detector.detect("", 123, 456)
    assert not result["is_spam"]
    assert result["stage"] == "skipped_short"


@pytest.mark.asyncio
async def test_whitespace_only_skip(mock_detector):
    """测试纯空格消息跳过检测"""
    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎返回非垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }
    )

    # 纯空格（使用原始长度）
    result = await detector.detect("   ", 123, 456)
    assert not result["is_spam"]
    assert result["stage"] == "skipped_short"
    assert result["details"]["original_length"] == 3


@pytest.mark.asyncio
async def test_emoji_only_skip(mock_detector):
    """测试纯表情消息跳过检测"""
    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎返回非垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }
    )

    # 纯表情
    result = await detector.detect("👍", 123, 456)
    assert not result["is_spam"]
    assert result["stage"] == "skipped_short"


@pytest.mark.asyncio
async def test_exactly_threshold(mock_detector):
    """测试长度恰好等于阈值"""
    detector, mock_rule_engine = mock_detector

    # Mock 规则引擎返回非垃圾
    mock_rule_engine.analyze = MagicMock(
        return_value={
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }
    )

    # 恰好达到 10 的标准化长度（20 个半角字符）
    result = await detector.detect("12345678901234567890", 123, 456)
    # 不应该跳过（标准化长度 >= 阈值）
    assert result["stage"] != "skipped_short"
