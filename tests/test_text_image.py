"""文字题面转图片渲染测试。

覆盖核心契约：
- 产出合法 PNG、尺寸在合理范围
- 每次调用随机化（同输入两次输出不同）
- 输入校验（空文本 / 超长拒绝）
- slider 图片尺寸稳定、非法位置拒绝
"""

from io import BytesIO

import pytest
from aiogram.types import BufferedInputFile
from PIL import Image

from src.services.text_image import render_slider_image, render_text_image

pytestmark = pytest.mark.unit


def _decode(photo: BufferedInputFile) -> Image.Image:
    return Image.open(BytesIO(photo.data))


def test_render_text_image_produces_valid_png() -> None:
    photo = render_text_image("一年有多少个月？", locale="zh-Hans")
    assert photo.data
    assert photo.filename == "verification-text.png"
    with _decode(photo) as image:
        assert image.format == "PNG"
        # 逻辑宽度固定 640，高度按内容自适应且有下限
        assert image.size[0] == 640
        assert 96 <= image.size[1] <= 1200


def test_render_text_image_is_randomized_per_call() -> None:
    """同输入两次渲染产出不同字节（字体/配色/噪声/布局随机）"""
    first = render_text_image("一年有多少个月？", locale="zh-Hans")
    second = render_text_image("一年有多少个月？", locale="zh-Hans")
    assert first.data != second.data


def test_render_text_image_accepts_long_english() -> None:
    """英文长句按词断行，不抛错且尺寸合理"""
    photo = render_text_image(
        "Which body part does an elephant mainly use to draw water and grasp food?",
        locale="en",
    )
    with _decode(photo) as image:
        assert image.size[0] == 640
        assert image.size[1] <= 1200


def test_render_text_image_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        render_text_image("   ")


def test_render_text_image_rejects_oversized_text() -> None:
    with pytest.raises(ValueError, match="上限"):
        render_text_image("x" * 257)


def test_render_slider_image_valid_positions() -> None:
    """四个位置均产出合法 PNG，尺寸稳定"""
    for position in range(4):
        photo = render_slider_image(position)
        assert photo.data
        assert photo.filename == "verification-slider.png"
        with _decode(photo) as image:
            assert image.format == "PNG"
            assert image.size == (640, 200)


def test_render_slider_image_is_randomized_per_call() -> None:
    first = render_slider_image(0)
    second = render_slider_image(0)
    assert first.data != second.data


@pytest.mark.parametrize("position", [-1, 4, 99])
def test_render_slider_image_rejects_invalid_position(position: int) -> None:
    with pytest.raises(ValueError, match="0-3"):
        render_slider_image(position)
