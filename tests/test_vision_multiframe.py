"""Vision 多图合并请求测试。

动画贴纸（TGS / WebM）抽样多帧后合并为一次 Vision 请求整体判定。覆盖：
- 三种 LLM 协议 adapter 按顺序展开多个图片 block（Anthropic 多图加 ``Image N:`` 标签）
- provider 只在多图时追加"同一条动画的多帧"说明，单图 user 文本不变
- SpamDetector.detect_images 读全部图片后只发起一次 Vision 调用，逐图检查大小上限
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ml.ai_contracts import VISION_RESULT_SCHEMA
from src.ml.ai_detector import (
    AIServiceConfig,
    AIServiceProvider,
    VisionServiceProvider,
    VisionUnsupportedError,
    _build_vision_user_text,
)
from src.ml.ai_protocols import (
    AnthropicMessagesAdapter,
    AnthropicOutputMode,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    StructuredOutputMode,
    VisionImage,
)

pytestmark = pytest.mark.unit

CHAT_ID = -100123
USER_ID = 42

_FRAME_1 = VisionImage(b64="ZnJhbWUx", mime="image/png")
_FRAME_2 = VisionImage(b64="ZnJhbWUy", mime="image/jpeg")
_FRAMES = [_FRAME_1, _FRAME_2]

_MULTIFRAME_HINT = "同一条动画贴纸/视频"


def _build(adapter, images):
    return adapter.build_vision_payload(
        "vision-model", "system", "inspect", images, "low", VISION_RESULT_SCHEMA, 512
    )


# ===== adapter 多图 block 展开 =====


def test_openai_chat_adapter_emits_one_image_block_per_frame() -> None:
    """Chat Completions：文本在前，图片按序在后，detail 对每张图独立生效。"""
    payload = _build(OpenAIChatAdapter(StructuredOutputMode.STRICT), _FRAMES)
    content = payload["messages"][1]["content"]

    assert [part["type"] for part in content] == ["text", "image_url", "image_url"]
    assert content[1]["image_url"] == {"url": "data:image/png;base64,ZnJhbWUx", "detail": "low"}
    assert content[2]["image_url"] == {"url": "data:image/jpeg;base64,ZnJhbWUy", "detail": "low"}


def test_openai_responses_adapter_emits_one_input_image_per_frame() -> None:
    """Responses API：input_text 在前，多个 input_image 按序在后。"""
    payload = _build(OpenAIResponsesAdapter(StructuredOutputMode.STRICT), _FRAMES)
    content = payload["input"][1]["content"]

    assert [part["type"] for part in content] == ["input_text", "input_image", "input_image"]
    assert content[1]["image_url"] == "data:image/png;base64,ZnJhbWUx"
    assert content[2]["image_url"] == "data:image/jpeg;base64,ZnJhbWUy"
    assert content[2]["detail"] == "low"


def test_anthropic_adapter_labels_frames_only_when_multiple() -> None:
    """Anthropic：多图时每张图前加 ``Image N:`` 标签；单图保持原有 [image, text] 结构。"""
    adapter = AnthropicMessagesAdapter(StructuredOutputMode.STRICT, AnthropicOutputMode.NATIVE)

    multi = _build(adapter, _FRAMES)["messages"][0]["content"]
    assert [part["type"] for part in multi] == ["text", "image", "text", "image", "text"]
    assert multi[0]["text"] == "Image 1:"
    assert multi[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "ZnJhbWUx"}
    assert multi[2]["text"] == "Image 2:"
    assert multi[3]["source"]["media_type"] == "image/jpeg"
    assert multi[-1]["text"] == "inspect"

    single = _build(adapter, [_FRAME_1])["messages"][0]["content"]
    assert [part["type"] for part in single] == ["image", "text"]


# ===== provider user 文本：多帧说明只在多图时出现 =====


def test_vision_user_text_adds_multiframe_hint_only_for_multiple_images() -> None:
    single = _build_vision_user_text(frame_count=1, caption=None, context_text=None)
    multi = _build_vision_user_text(frame_count=2, caption="emoji", context_text="ctx")

    assert _MULTIFRAME_HINT not in single
    assert _MULTIFRAME_HINT in multi
    assert "2 张图片" in multi
    # 段落顺序：上下文 → caption → 多帧说明 → 判定指令
    assert multi.index("【群组对话上下文】") < multi.index("【图片说明 / caption】")
    assert multi.index("【图片说明 / caption】") < multi.index(_MULTIFRAME_HINT)
    assert multi.rstrip().endswith("垃圾判定结果。")


async def test_provider_detect_image_sends_all_frames_in_one_call() -> None:
    """provider.detect_image 把整组图片原样交给一次 _call_api_vision。"""
    provider = VisionServiceProvider.__new__(VisionServiceProvider)
    AIServiceProvider.__init__(
        provider,
        "vision",
        AIServiceConfig(enabled=True, api_key="k", api_base="https://x.test/v1", max_retries=0),
    )
    provider._call_api_vision = AsyncMock(  # type: ignore[method-assign]
        return_value={"is_spam": False, "confidence": 0.1, "reason": "ok", "extracted_text": ""}
    )

    await provider.detect_image(_FRAMES, caption="emoji")

    provider._call_api_vision.assert_awaited_once()
    _, user_text, images = provider._call_api_vision.await_args.args
    assert tuple(images) == tuple(_FRAMES)
    assert _MULTIFRAME_HINT in user_text


async def test_provider_detect_image_rejects_empty_images() -> None:
    provider = VisionServiceProvider.__new__(VisionServiceProvider)
    AIServiceProvider.__init__(
        provider,
        "vision",
        AIServiceConfig(enabled=True, api_key="k", api_base="https://x.test/v1"),
    )
    with pytest.raises(ValueError, match="至少需要一张图片"):
        await provider.detect_image([])


# ===== SpamDetector.detect_images：多帧一次请求 =====


def _make_detector(vision_result: dict | None = None):
    """构造 SpamDetector（mock 全部依赖），返回 (detector, mock_ai)。"""
    with (
        patch("src.services.spam_detector.get_rule_engine"),
        patch("src.services.spam_detector.get_classifier"),
        patch("src.services.spam_detector.get_embedder"),
        patch("src.services.spam_detector.get_ai_detector") as mock_get_ai_detector,
    ):
        mock_ai = MagicMock()
        mock_ai.vision_enabled = True
        mock_ai.detect_image_with_context = AsyncMock(
            return_value=vision_result
            or {
                "is_spam": False,
                "confidence": 0.1,
                "stage": "ai_vision",
                "reasons": ["ok"],
                "details": {"extracted_text": ""},
            }
        )
        mock_get_ai_detector.return_value = mock_ai

        from src.services.spam_detector import SpamDetector

        return SpamDetector(), mock_ai


def _write_frames(tmp_path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        frame = tmp_path / f"frame_{index}.png"
        frame.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]) * 8)
        paths.append(str(frame))
    return paths


async def test_detect_images_reads_all_frames_and_calls_vision_once(tmp_path) -> None:
    detector, mock_ai = _make_detector()
    frame_paths = _write_frames(tmp_path, 2)

    with patch("src.services.spam_detector.get_resolver") as mock_get_resolver:
        mock_get_resolver.return_value.for_group = AsyncMock(return_value="en")
        result = await detector.detect_images(
            frame_paths, USER_ID, CHAT_ID, caption="emoji", skip_auto_train=True
        )

    mock_ai.detect_image_with_context.assert_awaited_once()
    images = mock_ai.detect_image_with_context.await_args.args[0]
    assert [image.mime for image in images] == ["image/png", "image/png"]
    assert mock_ai.detect_image_with_context.await_args.kwargs["locale"] == "en"
    assert result["details"]["image_count"] == 2


async def test_detect_images_spam_verdict_applies_to_whole_sticker(tmp_path) -> None:
    """多帧整体判定：AI 判垃圾时结果带回 Vision 阶段与识别文本。"""
    detector, _ = _make_detector(
        {
            "is_spam": True,
            "confidence": 0.95,
            "stage": "ai_vision",
            "reasons": ["博彩广告"],
            "details": {"extracted_text": "稳赚不赔 加微信 abc"},
        }
    )
    frame_paths = _write_frames(tmp_path, 2)

    with patch("src.services.spam_detector.get_resolver") as mock_get_resolver:
        mock_get_resolver.return_value.for_group = AsyncMock(return_value="zh-Hans")
        result = await detector.detect_images(frame_paths, USER_ID, CHAT_ID, skip_auto_train=True)

    assert result["is_spam"] is True
    assert result["stage"] == "ai_vision"
    assert result["details"]["recognized_text"] == "稳赚不赔 加微信 abc"
    assert result["details"]["sample_text"] == "稳赚不赔 加微信 abc"


async def test_detect_images_enforces_per_image_size_limit(tmp_path) -> None:
    """任一帧超过单图上限即整组放行（不发请求）。"""
    detector, mock_ai = _make_detector()
    frame_paths = _write_frames(tmp_path, 2)

    with (
        patch("src.services.spam_detector.get_resolver") as mock_get_resolver,
        patch("src.services.spam_detector.settings.ai_spam_vision_max_image_bytes", 4),
    ):
        mock_get_resolver.return_value.for_group = AsyncMock(return_value="en")
        result = await detector.detect_images(frame_paths, USER_ID, CHAT_ID)

    assert result["is_spam"] is False
    assert result["stage"] is None
    mock_ai.detect_image_with_context.assert_not_awaited()


async def test_detect_images_empty_paths_skips_vision() -> None:
    detector, mock_ai = _make_detector()

    result = await detector.detect_images([], USER_ID, CHAT_ID)

    assert result["is_spam"] is False
    mock_ai.detect_image_with_context.assert_not_awaited()


async def test_detect_images_size_limit_raises_inside_vision_path(tmp_path) -> None:
    """内部路径抛 VisionUnsupportedError，由入口统一转为放行（错误类型契约）。"""
    detector, _ = _make_detector()
    frame_paths = _write_frames(tmp_path, 1)

    with (
        patch("src.services.spam_detector.settings.ai_spam_vision_max_image_bytes", 4),
        pytest.raises(VisionUnsupportedError),
    ):
        await detector._detect_images_via_vision(
            image_paths=frame_paths,
            user_id=USER_ID,
            chat_id=CHAT_ID,
            caption=None,
            context_text=None,
            locale="en",
            activity=None,
            skip_auto_train=True,
        )
