"""检测豁免阈值 → 置信度修正区间的透传与改判边界测试。

覆盖：
- ``_resolve_activity_skip_threshold``：全局 / 群组 / 禁用三态解析
- ``SpamDetector``：阈值经公开入口（含 AI 合并分支）透传到活跃度调整；对数曲线下的改判边界
- 消息入口（文本 / 图片 / 贴纸 / 编辑文本）：达到阈值跳过检测，上下文记录与重构前一致；
  未达阈值时把有效阈值传给检测器
- ``on_edited_photo_message``：不做检测豁免，但同样传入有效阈值
"""

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.handlers import antispam

pytestmark = pytest.mark.unit

CHAT_ID = -100123
USER_ID = 42
TEXT = "这是一条长度足够参与垃圾检测的测试消息"


def _not_spam() -> dict:
    return {
        "is_spam": False,
        "confidence": 0.0,
        "original_confidence": 0.0,
        "activity_reduction": 0.0,
        "stage": None,
        "reasons": [],
        "details": {},
    }


def _make_detector(*, rule_confidence: float | None = None, ai_result: dict | None = None):
    """构造 SpamDetector：规则引擎按 rule_confidence 命中（None 为不命中），ML / Embedding 关闭。

    ai_result 非空时启用 AI，由 detect_with_context 返回该结果。
    """
    with (
        patch("src.services.spam_detector.get_rule_engine") as mock_get_rule,
        patch("src.services.spam_detector.get_classifier") as mock_get_cls,
        patch("src.services.spam_detector.get_embedder") as mock_get_emb,
        patch("src.services.spam_detector.get_ai_detector") as mock_get_ai,
    ):
        mock_get_rule.return_value.analyze = MagicMock(
            return_value={
                "is_spam": rule_confidence is not None,
                "confidence": rule_confidence or 0.0,
                "reasons": ["url"] if rule_confidence is not None else [],
                "details": {},
            }
        )
        mock_get_cls.return_value.is_trained = False
        mock_get_emb.return_value.is_initialized = False
        mock_get_ai.return_value.enabled = ai_result is not None
        mock_get_ai.return_value.detect_with_context = AsyncMock(return_value=ai_result)

        from src.services.spam_detector import SpamDetector

        return SpamDetector()


@pytest.fixture
def _detection_settings(mocker):
    """固定检测参数：改判阈值 0.7、最大修正 0.15，关闭短文本跳过与上下文调整"""
    mocker.patch("src.services.spam_detector.settings.spam_threshold_ml", 0.7)
    mocker.patch("src.services.spam_detector.settings.spam_min_text_length", 0)
    mocker.patch("src.services.spam_detector.settings.context_consistency_enabled", False)
    mocker.patch("src.services.activity.settings.activity_max_confidence_reduction", 0.15)


# ===== 检测器：阈值透传与改判边界 =====


@pytest.mark.usefixtures("_detection_settings")
@pytest.mark.parametrize(("activity", "expected_spam"), [(4, True), (5, False)])
async def test_rule_hit_cleared_from_activity_5_with_threshold_10(activity, expected_spam) -> None:
    """规则命中 0.80、阈值 10：活跃度 4 修正 0.090 仍判垃圾，5 修正 0.105 改判正常"""
    detector = _make_detector(rule_confidence=0.8)

    result = await detector.detect_with_ai_context(
        TEXT, USER_ID, CHAT_ID, activity=activity, activity_skip_threshold=10
    )

    assert result["is_spam"] is expected_spam
    assert result["activity_reduction"] == pytest.approx(0.15 * math.log(activity) / math.log(10))


@pytest.mark.usefixtures("_detection_settings")
async def test_rule_hit_not_adjusted_below_10_when_skip_disabled() -> None:
    """未启用豁免：活跃度 < 10 不修正（旧公式起点不变）"""
    detector = _make_detector(rule_confidence=0.8)

    result = await detector.detect_with_ai_context(TEXT, USER_ID, CHAT_ID, activity=5)

    assert result["is_spam"] is True
    assert result["confidence"] == pytest.approx(0.8)
    assert result["activity_reduction"] == 0.0


@pytest.mark.usefixtures("_detection_settings")
async def test_ai_only_hit_adjusted_with_skip_threshold(mocker) -> None:
    """传统未命中、AI 判垃圾 0.80：合并分支同样按阈值曲线修正（活跃度 5 → 改判正常）"""
    mocker.patch(
        "src.services.spam_detector.get_resolver",
        return_value=MagicMock(for_group=AsyncMock(return_value="zh-Hans")),
    )
    ai_result = {
        "is_spam": True,
        "confidence": 0.8,
        "stage": "ai_api",
        "reasons": [],
        "details": {},
    }
    detector = _make_detector(ai_result=ai_result)

    result = await detector.detect_with_ai_context(
        TEXT, USER_ID, CHAT_ID, activity=5, activity_skip_threshold=10, skip_auto_train=True
    )

    assert result["stage"] == "ai_api"
    assert result["is_spam"] is False


# ===== 有效阈值解析 =====


@pytest.mark.parametrize(
    ("global_threshold", "group", "expected"),
    [
        (30, SimpleNamespace(activity_skip_threshold=50), (30, "全局配置")),
        (0, SimpleNamespace(activity_skip_threshold=15), (15, "群组配置")),
        (0, None, (0, "群组配置")),
        (-1, SimpleNamespace(activity_skip_threshold=15), (0, "全局禁用")),
    ],
)
def test_resolve_activity_skip_threshold(mocker, global_threshold, group, expected) -> None:
    """全局 > 0 覆盖群组；= 0 取群组配置（未建组为 0）；< 0 全局禁用"""
    mocker.patch.object(antispam.settings, "activity_skip_spam_check_threshold", global_threshold)

    assert antispam._resolve_activity_skip_threshold(group) == expected


# ===== 消息入口：跳过判断与阈值传参 =====


def _message(*, text: str | None = TEXT, caption: str | None = None) -> MagicMock:
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID, type="supergroup", title="Test", description=None)
    message.from_user = SimpleNamespace(id=USER_ID, username=None)
    message.text = text
    message.caption = caption
    message.message_id = 1
    return message


@pytest.fixture
def handler_env(mocker):
    """打桩入口依赖：前置检查通过、全局阈值 0（取群组阈值 10）、关闭上下文

    返回 (检测器 mock, 上下文记录 mock)。
    """
    group = SimpleNamespace(
        antispam_enabled=True,
        activity_enabled=True,
        activity_skip_threshold=10,
        spam_confirm_enabled=True,
    )
    mocker.patch.object(antispam, "_run_message_prechecks", new=AsyncMock(return_value=None))
    mocker.patch.object(antispam.GroupRepository, "get", new=AsyncMock(return_value=group))
    mocker.patch.object(
        antispam.GroupRepository, "get_or_create", new=AsyncMock(return_value=group)
    )
    mocker.patch.object(antispam, "check_non_text_message", new=AsyncMock(return_value=False))
    mocker.patch.object(antispam.settings, "activity_skip_spam_check_threshold", 0)
    mocker.patch.object(antispam.settings, "context_enabled", False)
    record_context = mocker.patch.object(antispam.ContextService, "record_message", new=AsyncMock())
    detector = MagicMock()
    detector.detect_with_ai_context = AsyncMock(return_value=_not_spam())
    detector.detect_images = AsyncMock(return_value=_not_spam())
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    return detector, record_context


def _photo_message() -> MagicMock:
    message = _message(text=None)
    message.content_type = "photo"
    message.photo = [SimpleNamespace(file_id="photo-file")]
    return message


def _sticker_message() -> MagicMock:
    message = _message(text=None)
    message.content_type = "sticker"
    return message


@pytest.fixture
def plain_text(mocker):
    """普通文本消息：非外部转发、无链接，走 record_text_message 计分分支"""
    mocker.patch.object(antispam, "is_external_forward", return_value=False)
    mocker.patch.object(antispam, "has_url_entities", return_value=False)


@pytest.mark.usefixtures("plain_text")
async def test_on_message_skips_detection_at_threshold(mocker, handler_env) -> None:
    """活跃度达到阈值：跳过检测，只记上下文"""
    mocker.patch.object(
        antispam.ActivityService, "record_text_message", new=AsyncMock(return_value=10)
    )

    await antispam.on_message(_message(), MagicMock())

    detector, record_context = handler_env
    detector.detect_with_ai_context.assert_not_awaited()
    record_context.assert_awaited_once()


@pytest.mark.usefixtures("plain_text")
async def test_on_message_passes_effective_threshold_below_it(mocker, handler_env) -> None:
    """活跃度未达阈值：照常检测，并把有效阈值传给检测器决定修正区间"""
    mocker.patch.object(
        antispam.ActivityService, "record_text_message", new=AsyncMock(return_value=9)
    )

    await antispam.on_message(_message(), MagicMock())

    detector, _ = handler_env
    kwargs = detector.detect_with_ai_context.await_args.kwargs
    assert (kwargs["activity"], kwargs["activity_skip_threshold"]) == (9, 10)


@pytest.mark.parametrize(
    ("handler", "make_message", "records_context"),
    [
        (antispam.on_photo_message, _photo_message, True),
        (antispam.on_sticker_message, _sticker_message, True),
        (antispam.on_edited_text_message, _message, False),
    ],
    ids=["photo", "sticker", "edited_text"],
)
async def test_other_handlers_skip_detection_at_threshold(
    mocker, handler_env, handler, make_message, records_context
) -> None:
    """活跃度达到阈值即跳过检测；上下文记录与重构前一致（编辑文本不记录）"""
    mocker.patch.object(antispam.ActivityService, "get_activity", new=AsyncMock(return_value=10))

    await handler(make_message(), MagicMock())

    detector, record_context = handler_env
    detector.detect_with_ai_context.assert_not_awaited()
    detector.detect_images.assert_not_awaited()
    assert record_context.await_count == (1 if records_context else 0)


@pytest.mark.parametrize(
    ("handler", "make_message", "detect_method"),
    [
        (antispam.on_photo_message, _photo_message, "detect_images"),
        (antispam.on_edited_text_message, _message, "detect_with_ai_context"),
    ],
    ids=["photo", "edited_text"],
)
async def test_other_handlers_pass_effective_threshold_below_it(
    mocker, handler_env, handler, make_message, detect_method
) -> None:
    """活跃度未达阈值：照常检测，并把有效阈值传给检测器决定修正区间"""
    mocker.patch.object(antispam.ActivityService, "get_activity", new=AsyncMock(return_value=9))
    bot = MagicMock()
    bot.download = AsyncMock()

    await handler(make_message(), bot)

    detector, _ = handler_env
    kwargs = getattr(detector, detect_method).await_args.kwargs
    assert (kwargs["activity"], kwargs["activity_skip_threshold"]) == (9, 10)


async def test_on_edited_photo_passes_threshold_without_skipping(mocker, handler_env) -> None:
    """编辑图片 caption：不做豁免（活跃度超过阈值仍检测），同样传入有效阈值"""
    mocker.patch.object(antispam.ActivityService, "get_activity", new=AsyncMock(return_value=50))

    await antispam.on_edited_photo_message(_message(text=None, caption=TEXT), MagicMock())

    detector, _ = handler_env
    kwargs = detector.detect_with_ai_context.await_args.kwargs
    assert (kwargs["activity"], kwargs["activity_skip_threshold"]) == (50, 10)
