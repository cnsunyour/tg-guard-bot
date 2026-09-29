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


def test_render_text_image_chinese_text_with_non_chinese_locale() -> None:
    """非 zh locale（如 en 群）+ 中文题面：CJK 池被 locale 过滤清空时必须
    放宽为全部 CJK 字形渲染，而不是回退仅拉丁的 PIL 默认字体（整行豆腐）。"""
    from src.services.text_image import render_text_image as _r  # noqa: F401  # 确保模块已初始化

    png = render_text_image("中文测试题面：请选择正确答案", locale="en")
    raw = png.file.getvalue() if hasattr(png, "file") else png.data
    image = Image.open(BytesIO(raw)).convert("L")
    width, height = image.size
    dark = sum(1 for px in image.getdata() if px < 100)
    # 豆腐帧场景深色像素集中在极少方块；正常字形渲染占比显著更高
    assert (
        dark / (width * height) > 0.02
    ), f"非 zh locale 的中文题面渲染疑似豆腐（深色像素占比 {dark / (width * height):.2%}）"


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
