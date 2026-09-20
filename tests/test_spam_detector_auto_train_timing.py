"""AI 自动样本入库时机 + 群组上下文完整输出回归测试。

- AI 正样本必须在活跃度/上下文调整全部完成、且最终仍判垃圾时才入库
- 被调整改判为正常的消息不进入垃圾训练集
- 负样本沿用原语义（AI 判正常且传统未判垃圾）
- 确认模式 / 降级路径 / 传统判垃圾均不产生 AI 样本
- _merge_detection_results 为纯合并，无入库副作用
- format_context_for_ai 输出全部缓存消息（时间正序），不再截断为 5 条
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.context_service import ContextService

pytestmark = pytest.mark.unit

TEXT = "这是一条用于检测样本入库时机的中文消息"
USER_ID = 42
CHAT_ID = -100123


def _result(is_spam: bool, stage: str) -> dict:
    return {
        "is_spam": is_spam,
        "confidence": 0.95 if is_spam else 0.05,
        "original_confidence": 0.0,
        "activity_reduction": 0.0,
        "stage": stage,
        "reasons": [],
        "details": {},
    }


def _mock_kwargs(outcome: dict | Exception) -> dict:
    """异常 → side_effect 抛出；正常结果 → return_value。"""
    if isinstance(outcome, Exception):
        return {"side_effect": outcome}
    return {"return_value": outcome}


def _make_detector(*, traditional: dict | Exception, ai: dict | Exception):
    """构造 SpamDetector：传统链路与 AI 链路结果可注入，入库 handler 打桩。"""
    with (
        patch("src.services.spam_detector.get_rule_engine"),
        patch("src.services.spam_detector.get_classifier"),
        patch("src.services.spam_detector.get_embedder"),
        patch("src.services.spam_detector.get_ai_detector"),
    ):
        from src.services.spam_detector import SpamDetector

        detector = SpamDetector()

    detector.detect = AsyncMock(**_mock_kwargs(traditional))
    detector.ai_detector.enabled = True
    detector.ai_detector.detect = AsyncMock(**_mock_kwargs(ai))
    detector.ai_detector.detect_with_context = AsyncMock(**_mock_kwargs(ai))
    detector.embedder.is_initialized = True
    detector.embedder.compute_similarity = AsyncMock(return_value=0.0)
    detector.embedder.detect_context_consistency = AsyncMock(return_value=(True, 0.9))
    detector._handle_ai_spam_detection = AsyncMock()
    detector._handle_ai_negative_detection = AsyncMock()
    return detector


@contextlib.contextmanager
def _detection_env(*, context_reduction: float = 0.1, activity_reduction: float = 0.0):
    """固定阈值与调整幅度：ML 阈值 0.7、Embedding 阈值 0.75，locale 解析打桩。"""
    with (
        patch("src.services.spam_detector.settings.spam_min_text_length", 0),
        patch("src.services.spam_detector.settings.spam_threshold_ml", 0.7),
        patch("src.services.spam_detector.settings.spam_threshold_embedding", 0.75),
        patch("src.services.spam_detector.settings.context_consistency_enabled", True),
        patch(
            "src.services.spam_detector.settings.context_confidence_reduction", context_reduction
        ),
        patch(
            "src.services.activity.ActivityService.calculate_confidence_reduction",
            return_value=activity_reduction,
        ),
        patch(
            "src.services.spam_detector.get_resolver",
            return_value=MagicMock(for_group=AsyncMock(return_value="zh-Hans")),
        ),
    ):
        yield


CONTEXT_MESSAGES = [{"text": "群组话题"} for _ in range(3)]


# ===== 正样本：活跃度改判后不入库 =====
@pytest.mark.parametrize("with_context", [False, True], ids=["plain", "context"])
async def test_positive_sample_skipped_when_activity_adjustment_clears_spam(with_context):
    """AI 判垃圾 0.95，活跃度减 0.3 → 0.65 < 0.7 改判正常 → 不入正样本。"""
    detector = _make_detector(traditional=_result(False, "rule_engine"), ai=_result(True, "ai_api"))
    with _detection_env(activity_reduction=0.3):
        if with_context:
            result = await detector.detect_with_ai_context(
                TEXT, USER_ID, CHAT_ID, activity=100, context_messages=CONTEXT_MESSAGES
            )
        else:
            result = await detector.detect_with_ai(TEXT, USER_ID, CHAT_ID, activity=100)

    assert result["is_spam"] is False
    detector._handle_ai_spam_detection.assert_not_awaited()
    detector._handle_ai_negative_detection.assert_not_awaited()


# ===== 正样本：上下文改判后不入库（仅 context 入口） =====
async def test_positive_sample_skipped_when_context_adjustment_clears_spam():
    """AI 判垃圾 0.95，话题一致减 0.3 → 0.65 < 0.75 改判正常 → 不入正样本。"""
    detector = _make_detector(traditional=_result(False, "rule_engine"), ai=_result(True, "ai_api"))
    with _detection_env(context_reduction=0.3):
        result = await detector.detect_with_ai_context(
            TEXT, USER_ID, CHAT_ID, context_messages=CONTEXT_MESSAGES
        )

    assert result["is_spam"] is False
    detector._handle_ai_spam_detection.assert_not_awaited()
    detector._handle_ai_negative_detection.assert_not_awaited()


# ===== 正样本：最终仍判垃圾 → 入库，且在调整之后 =====
@pytest.mark.parametrize("with_context", [False, True], ids=["plain", "context"])
@pytest.mark.parametrize("traditional_failed", [False, True], ids=["trad_ok", "trad_failed"])
async def test_positive_sample_collected_after_adjustments_when_still_spam(
    with_context, traditional_failed
):
    """调整幅度小（0.1）仍 ≥ 阈值 → 正样本入库；入库发生在调整之后，置信度为 AI 原值。"""
    traditional = RuntimeError("传统失败") if traditional_failed else _result(False, "rule_engine")
    ai = _result(True, "ai_api")
    detector = _make_detector(traditional=traditional, ai=ai)

    order: list[str] = []
    original_activity = detector._apply_activity_adjustment
    original_context = detector._apply_context_adjustment

    def spy_activity(*args):
        order.append("activity")
        return original_activity(*args)

    async def spy_context(*args):
        order.append("context")
        return await original_context(*args)

    detector._apply_activity_adjustment = MagicMock(side_effect=spy_activity)
    detector._apply_context_adjustment = AsyncMock(side_effect=spy_context)
    detector._handle_ai_spam_detection = AsyncMock(side_effect=lambda *a: order.append("sample"))

    with _detection_env(context_reduction=0.1, activity_reduction=0.1):
        if with_context:
            result = await detector.detect_with_ai_context(
                TEXT, USER_ID, CHAT_ID, activity=100, context_messages=CONTEXT_MESSAGES
            )
        else:
            result = await detector.detect_with_ai(TEXT, USER_ID, CHAT_ID, activity=100)

    assert result["is_spam"] is True
    detector._handle_ai_spam_detection.assert_awaited_once_with(TEXT, ai, USER_ID)
    # 入库置信度保留 AI 原值（活跃度/上下文调整只作用于最终结果副本，不回写 AI 原始结果）
    assert detector._handle_ai_spam_detection.await_args.args[1]["confidence"] == 0.95
    assert order[-1] == "sample"
    assert "activity" in order
    if with_context:
        assert "context" in order


# ===== 传统判垃圾被上下文改判正常：快照保证不误入 AI 正样本 =====
async def test_no_ai_sample_when_traditional_spam_cleared_by_context():
    """传统 0.95 判垃圾，上下文减 0.3 改判正常；合并采用的是传统结果，AI 样本不入库。"""
    detector = _make_detector(traditional=_result(True, "rule_engine"), ai=_result(True, "ai_api"))
    with _detection_env(context_reduction=0.3):
        result = await detector.detect_with_ai_context(
            TEXT, USER_ID, CHAT_ID, context_messages=CONTEXT_MESSAGES
        )

    assert result["is_spam"] is False
    detector._handle_ai_spam_detection.assert_not_awaited()
    detector._handle_ai_negative_detection.assert_not_awaited()


# ===== 负样本：AI 判正常且传统未判垃圾 → 入库 =====
@pytest.mark.parametrize("with_context", [False, True], ids=["plain", "context"])
@pytest.mark.parametrize("traditional_failed", [False, True], ids=["trad_ok", "trad_failed"])
async def test_negative_sample_collected_when_ai_normal(with_context, traditional_failed):
    traditional = RuntimeError("传统失败") if traditional_failed else _result(False, "rule_engine")
    ai = _result(False, "ai_api")
    detector = _make_detector(traditional=traditional, ai=ai)

    with _detection_env():
        if with_context:
            await detector.detect_with_ai_context(
                TEXT, USER_ID, CHAT_ID, context_messages=CONTEXT_MESSAGES
            )
        else:
            await detector.detect_with_ai(TEXT, USER_ID, CHAT_ID)

    detector._handle_ai_negative_detection.assert_awaited_once_with(TEXT, ai, USER_ID)
    detector._handle_ai_spam_detection.assert_not_awaited()


# ===== 传统判垃圾：采用传统结果，AI 样本不入库 =====
@pytest.mark.parametrize("ai_spam", [True, False])
async def test_no_ai_sample_when_traditional_flags_spam(ai_spam):
    detector = _make_detector(
        traditional=_result(True, "rule_engine"), ai=_result(ai_spam, "ai_api")
    )
    with _detection_env():
        result = await detector.detect_with_ai_context(
            TEXT, USER_ID, CHAT_ID, context_messages=CONTEXT_MESSAGES
        )

    assert result["stage"] == "rule_engine"
    detector._handle_ai_spam_detection.assert_not_awaited()
    detector._handle_ai_negative_detection.assert_not_awaited()


# ===== 确认模式：任何情况不入库 =====
@pytest.mark.parametrize("ai_spam", [True, False])
@pytest.mark.parametrize("with_context", [False, True], ids=["plain", "context"])
async def test_confirmation_mode_never_collects_samples(ai_spam, with_context):
    detector = _make_detector(
        traditional=_result(False, "rule_engine"), ai=_result(ai_spam, "ai_api")
    )
    with _detection_env():
        if with_context:
            await detector.detect_with_ai_context(
                TEXT, USER_ID, CHAT_ID, context_messages=CONTEXT_MESSAGES, skip_auto_train=True
            )
        else:
            await detector.detect_with_ai(TEXT, USER_ID, CHAT_ID, skip_auto_train=True)

    detector._handle_ai_spam_detection.assert_not_awaited()
    detector._handle_ai_negative_detection.assert_not_awaited()


# ===== AI 失败：无样本 =====
async def test_no_sample_when_ai_failed():
    detector = _make_detector(traditional=_result(False, "rule_engine"), ai=RuntimeError("AI 失败"))
    with _detection_env():
        await detector.detect_with_ai(TEXT, USER_ID, CHAT_ID)

    detector._handle_ai_spam_detection.assert_not_awaited()
    detector._handle_ai_negative_detection.assert_not_awaited()


# ===== 入库失败不影响检测结果 =====
async def test_sample_failure_does_not_change_result():
    detector = _make_detector(traditional=_result(False, "rule_engine"), ai=_result(True, "ai_api"))
    detector._handle_ai_spam_detection.side_effect = RuntimeError("入库失败")
    with _detection_env():
        result = await detector.detect_with_ai(TEXT, USER_ID, CHAT_ID)

    assert result["is_spam"] is True
    detector.detect.assert_awaited_once()  # 未触发降级重检


# ===== 合并函数无入库副作用 =====
@pytest.mark.parametrize("ai_spam", [True, False])
async def test_merge_has_no_sample_side_effects(ai_spam):
    detector = _make_detector(
        traditional=_result(False, "rule_engine"), ai=_result(ai_spam, "ai_api")
    )
    await detector._merge_detection_results(None, _result(ai_spam, "ai_api"), USER_ID)
    await detector._merge_detection_results(
        _result(False, "rule_engine"), _result(ai_spam, "ai_api"), USER_ID
    )
    detector._handle_ai_spam_detection.assert_not_awaited()
    detector._handle_ai_negative_detection.assert_not_awaited()


# ===== 上下文格式化：全部消息、时间正序、排除当前消息 =====
@pytest.mark.parametrize("count", [3, 10, 12])
@pytest.mark.parametrize("current_message_id", [None, 7])
def test_format_context_keeps_all_messages_in_chronological_order(count, current_message_id):
    """Redis 缓存最新在前；输出应为最老在前的全部消息，不再截断为 5 条。"""
    recent = [
        {
            "user_id": USER_ID,
            "user_name": "群友",
            "text": f"历史消息 {i}",
            "timestamp": i,
            "message_id": i,
        }
        for i in range(count, 0, -1)
    ]

    formatted = ContextService.format_context_for_ai(
        {"reply_chain": [], "recent_messages": recent}, TEXT, current_message_id
    )

    expected_lines = [f"群友: 历史消息 {i}" for i in range(1, count + 1) if i != current_message_id]
    assert formatted.splitlines() == [
        "",
        "【群组最近对话】",
        *expected_lines,
        "",
        "【待检测消息】",
        TEXT,
    ]
