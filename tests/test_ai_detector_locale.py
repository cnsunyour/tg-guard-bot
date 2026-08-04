"""AI 检测器 reason locale 化测试（3c14）。

覆盖：
- locale → 语言名 / 兜底文案映射（纯函数）
- _build_system_prompt 按 locale 注入语言约束
- provider detect 透传 locale 给 _call_api + _process_result
- HybridAIDetector detect/detect_with_context 透传（含 backup 回退、无 context 降级）
- AISpamDetector 未启用兜底按 locale
- Vision _process_vision_result locale 兜底
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ml.ai_detector import (
    HybridAIDetector,
    PrimaryAIServiceProvider,
    _ai_disabled_reason,
    _build_system_prompt,
    _reason_fallback,
    _reason_language_name,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def primary_provider():
    """主服务商 fixture（mock _create_client 避免实际创建 httpx client）。"""
    provider = PrimaryAIServiceProvider()
    with patch.object(provider, "_create_client", return_value=MagicMock()):
        yield provider


# ===== locale → 语言名映射 =====
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh-Hans", "简体中文"),
        ("zh-Hant", "繁體中文"),
        ("en", "English"),
        (None, "简体中文"),  # 默认
        ("ja-JP", "简体中文"),  # 未知 locale 降级
        ("", "简体中文"),  # 空串降级
    ],
)
def test_reason_language_name_maps_locale(locale: str | None, expected: str) -> None:
    assert _reason_language_name(locale) == expected


# ===== locale → AI 未返回 reason 兜底 =====
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh-Hans", "未提供判断理由"),
        ("zh-Hant", "未提供判斷理由"),
        ("en", "No reason provided"),
        (None, "未提供判断理由"),
        ("fr", "未提供判断理由"),  # 未知降级
    ],
)
def test_reason_fallback_maps_locale(locale: str | None, expected: str) -> None:
    assert _reason_fallback(locale) == expected


# ===== locale → AI 未启用兜底 =====
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh-Hans", "AI 检测未启用"),
        ("zh-Hant", "AI 偵測未啟用"),
        ("en", "AI detection disabled"),
        (None, "AI 检测未启用"),
        ("de", "AI 检测未启用"),  # 未知降级
    ],
)
def test_ai_disabled_reason_maps_locale(locale: str | None, expected: str) -> None:
    assert _ai_disabled_reason(locale) == expected


# ===== _build_system_prompt 按 locale 注入语言约束 =====
def test_build_system_prompt_injects_language_directive() -> None:
    """非中文 locale → prompt 含语言约束段 + 对应语言名。"""
    base = "基础 prompt"
    result_zh = _build_system_prompt(base, "zh-Hans")
    result_en = _build_system_prompt(base, "en")
    result_hant = _build_system_prompt(base, "zh-Hant")
    result_none = _build_system_prompt(base, None)

    assert "简体中文" in result_zh
    assert "English" in result_en
    assert "繁體中文" in result_hant
    assert "简体中文" in result_none  # None 默认中文
    # 约束段标识统一存在
    for r in (result_zh, result_en, result_hant):
        assert "reason 输出语言" in r
    # 基础 prompt 保留
    assert result_en.startswith("基础 prompt")


def test_build_system_prompt_unknown_locale_falls_back_to_chinese() -> None:
    """未知 locale → 降级简体中文。"""
    result = _build_system_prompt("base", "klingon")
    assert "简体中文" in result


# ===== provider detect 透传 locale 给 _call_api =====
async def test_provider_detect_passes_locale_to_call_api(primary_provider) -> None:
    """PrimaryAIServiceProvider.detect(text, locale) → _call_api 收到 locale。"""
    primary_provider._call_api = AsyncMock(
        return_value={"is_spam": True, "confidence": 0.9, "reason": "ad promotion"}
    )
    # 跳过文本长度截断影响
    await primary_provider.detect("spam text", locale="en")

    primary_provider._call_api.assert_awaited_once()
    call_args = primary_provider._call_api.await_args
    # _call_api(text, use_context_prompt, locale)
    assert call_args.args[0] == "spam text"
    assert call_args.args[2] == "en"  # locale 透传


async def test_provider_detect_locale_none_default(primary_provider) -> None:
    """locale=None → _call_api 收到 None（默认中文）。"""
    primary_provider._call_api = AsyncMock(
        return_value={"is_spam": False, "confidence": 0.1, "reason": "normal"}
    )
    await primary_provider.detect("text")
    assert primary_provider._call_api.await_args.args[2] is None


# ===== _process_result locale 兜底 =====
async def test_process_result_uses_locale_fallback_when_reason_missing(primary_provider) -> None:
    """AI 未返回 reason → 按 locale 兜底。"""
    # reason 缺失 → 兜底
    result = primary_provider._process_result({"is_spam": True, "confidence": 0.9}, "en")
    assert result.reasons[0] == "No reason provided"

    result_zh = primary_provider._process_result({"is_spam": True, "confidence": 0.9}, "zh-Hant")
    assert result_zh.reasons[0] == "未提供判斷理由"


async def test_process_result_keeps_ai_reason_when_provided(primary_provider) -> None:
    """AI 返回 reason → 保留原值（不兜底）。"""
    result = primary_provider._process_result(
        {"is_spam": True, "confidence": 0.9, "reason": "spam ad"}, "en"
    )
    assert result.reasons[0] == "spam ad"


# ===== _process_vision_result locale 兜底 =====
async def test_process_vision_result_uses_locale_fallback(primary_provider) -> None:
    """Vision 未返回 reason → 按 locale 兜底。"""
    result = primary_provider._process_vision_result({"is_spam": True, "confidence": 0.9}, "en")
    assert result.reasons[0] == "No reason provided"


# ===== HybridAIDetector detect 透传 locale（primary 路径）=====
async def test_hybrid_detect_passes_locale_to_primary() -> None:
    """HybridAIDetector.detect(text, locale) → primary.detect 收到 locale=locale。"""
    detector = HybridAIDetector()
    detector.primary = MagicMock()
    detector.primary.is_available = True
    detector._is_circuit_open = MagicMock(return_value=False)
    detector._record_success = MagicMock()

    from src.ml.ai_detector import AIDetectionResult

    detector.primary.detect = AsyncMock(
        return_value=AIDetectionResult(
            is_spam=True, confidence=0.9, provider="primary", reasons=["ad"]
        )
    )
    detector.primary.name = "primary"
    detector._get_success_rate = MagicMock(return_value=1.0)

    await detector.detect("text", "en")

    detector.primary.detect.assert_awaited_once_with("text", locale="en")


async def test_hybrid_detect_passes_locale_to_backup_on_primary_failure() -> None:
    """primary 失败 → backup.detect 收到同样的 locale。"""
    detector = HybridAIDetector()
    detector.primary = MagicMock()
    detector.primary.is_available = True
    detector.primary.name = "primary"
    detector.primary._format_error = MagicMock(return_value="err")
    detector.primary.request_client_rebuild = MagicMock()
    detector.primary.close = AsyncMock()
    detector.backup = MagicMock()
    detector.backup.is_available = True
    detector.backup.name = "backup"
    detector.backup._format_error = MagicMock(return_value="err")

    detector._is_circuit_open = MagicMock(return_value=False)
    detector._record_failure = MagicMock()
    detector._record_success = MagicMock()
    detector._get_success_rate = MagicMock(return_value=1.0)

    from src.ml.ai_detector import AIDetectionResult, AIServiceError

    # primary 抛 AIServiceError → 切 backup
    detector.primary.detect = AsyncMock(side_effect=AIServiceError("primary", "fail"))
    detector.backup.detect = AsyncMock(
        return_value=AIDetectionResult(
            is_spam=True, confidence=0.85, provider="backup", reasons=["ad"]
        )
    )

    await detector.detect("text", "zh-Hant")

    detector.primary.detect.assert_awaited_once_with("text", locale="zh-Hant")
    detector.backup.detect.assert_awaited_once_with("text", locale="zh-Hant")


# ===== detect_with_context 无 context 降级透传 locale =====
async def test_hybrid_detect_with_context_no_context_delegates_to_detect_with_locale() -> None:
    """context_text=None → detect_with_context 降级调 detect，locale 透传。"""
    detector = HybridAIDetector()
    detector.detect = AsyncMock(
        return_value={"is_spam": False, "confidence": 0.1, "reasons": [], "details": {}}
    )

    await detector.detect_with_context("text", None, "en")

    detector.detect.assert_awaited_once_with("text", "en")


# ===== AISpamDetector 未启用兜底按 locale =====
async def test_ai_spam_detector_disabled_returns_locale_reason() -> None:
    """AI 未启用 → reasons 用 _ai_disabled_reason(locale)。"""
    from src.ml.ai_detector import AISpamDetector

    with patch("src.ml.ai_detector.HybridAIDetector") as mock_hybrid_class:
        mock_hybrid = MagicMock()
        mock_hybrid.primary.is_available = False  # enabled = False
        mock_hybrid_class.return_value = mock_hybrid

        detector = AISpamDetector()
        assert detector.enabled is False

        result_en = await detector.detect("text", "en")
        assert result_en["reasons"] == ["AI detection disabled"]

        result_hant = await detector.detect("text", "zh-Hant")
        assert result_hant["reasons"] == ["AI 偵測未啟用"]

        # detect_with_context 同理
        result_ctx = await detector.detect_with_context("text", "ctx", "en")
        assert result_ctx["reasons"] == ["AI detection disabled"]
