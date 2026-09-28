"""文字题面转图片渲染。

把验证题面（数学表达式 / 问答问题 / 表情描述 / 滑块方格）渲染成 PNG 图片，
文本层（caption）只保留信封与说明，题面信息不再以文本形式暴露。

每次调用独立随机：字体、字号、配色、行距、水平偏移、行旋转与浅背景噪声，
作为对抗摩擦提高模板匹配 / 原图缓存类自动化的成本（不针对通用视觉模型）。
"""

import io
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import cast

from aiogram.types import BufferedInputFile
from loguru import logger
from PIL import Image, ImageDraw, ImageFont

# 2x 渲染后 Lanczos 缩到 1x，改善边缘抗锯齿（Telegram photo 压缩前保留细节）
_SCALE = 2
_LOGICAL_WIDTH = 640
# 字号档位（逻辑 px）：离散取值让字体缓存命中率最大化
_FONT_SIZES = (44, 42, 40, 38, 36)
_MAX_TEXT_CHARS = 256

# 英文单词为一个词元（词内不断行），空白单独成词元保留分隔，其余逐字符
_WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+|\s+|\S")

# 浅背景配色对（背景, 前景, 噪声）——低饱和浅底 + 深文字，保证可读性
_PALETTES: tuple[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]], ...] = (
    ((246, 248, 252), (27, 38, 54), (224, 229, 238)),
    ((255, 248, 232), (67, 46, 22), (241, 225, 192)),
    ((239, 250, 246), (18, 62, 53), (213, 237, 227)),
    ((245, 241, 255), (47, 28, 73), (226, 216, 245)),
    ((239, 247, 255), (16, 42, 67), (213, 230, 245)),
    ((255, 241, 245), (74, 29, 47), (241, 215, 224)),
)


@dataclass(frozen=True, slots=True)
class _FontSpec:
    """候选字体：文件路径 + ttc 集合内 face index + 类别 + 适用的 locale 集合"""

    path: Path
    index: int
    # "cjk" 含 CJK 与常用符号（÷×±）；"latin" 西文优先（math 表达式 / en 题面）
    kind: str
    # 空 frozenset 表示对所有 locale 可用
    locales: frozenset[str] = frozenset()

    def supports(self, locale: str) -> bool:
        return not self.locales or locale in self.locales


def _noto_ttc_index(locale: str) -> int:
    """Debian Noto CJK ttc 的 face 顺序惯例 JP=0/KR=1/SC=2/TC=3。

    具体顺序随发行包版本可能变化，加载时以 getname() 日志留证据核对。
    """
    normalized = locale.lower()
    if "hant" in normalized or "tw" in normalized or "hk" in normalized:
        return 3
    return 2


def _font_candidates() -> tuple[_FontSpec, ...]:
    """全部候选字体（Linux 容器 + macOS 本地开发）。

    macOS ttc index 为本机实测（getname 确认）；Linux 以 Noto 惯例顺序为准。
    字形按 locale 严格过滤：简体字形（Hiragino GB/STHeiti/Noto SC）不服务
    zh-Hant，避免繁体题面渲染出简体字形。拉丁池不限 locale——纯拉丁题面
    （math 表达式、en 问答）用西文字形渲染，CJK 字体不覆盖的字符也有兜底。
    """
    hant = frozenset({"zh-Hant"})
    hans = frozenset({"zh-Hans"})
    return (
        # --- 拉丁池（容器 fonts-dejavu-core 新旧镜像都有；math/en 题面优先）---
        _FontSpec(Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), 0, "latin"),
        _FontSpec(Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), 0, "latin"),
        _FontSpec(Path("/System/Library/Fonts/Supplemental/Arial.ttf"), 0, "latin"),
        _FontSpec(Path("/System/Library/Fonts/Helvetica.ttc"), 0, "latin"),
        # --- Debian: fonts-noto-cjk（index 2=SC 仅简体；index 3=TC 仅繁体）---
        _FontSpec(
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            _noto_ttc_index("zh"),
            "cjk",
            hans,
        ),
        _FontSpec(
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
            _noto_ttc_index("zh"),
            "cjk",
            hans,
        ),
        _FontSpec(
            Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
            _noto_ttc_index("zh"),
            "cjk",
            hans,
        ),
        _FontSpec(Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), 3, "cjk", hant),
        # --- macOS 本地开发（index 已实测）---
        _FontSpec(Path("/System/Library/Fonts/Hiragino Sans GB.ttc"), 0, "cjk", hans),  # W3
        _FontSpec(Path("/System/Library/Fonts/Hiragino Sans GB.ttc"), 2, "cjk", hans),  # W6
        _FontSpec(
            Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 6, "cjk", hans
        ),  # SC Regular
        _FontSpec(Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 1, "cjk", hans),  # SC Bold
        _FontSpec(
            Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 3, "cjk", hans
        ),  # SC Light
        _FontSpec(
            Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 0, "cjk", hans
        ),  # SC Black
        _FontSpec(
            Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 7, "cjk", hant
        ),  # TC Regular
        _FontSpec(Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 2, "cjk", hant),  # TC Bold
        _FontSpec(
            Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 5, "cjk", hant
        ),  # TC Light
        _FontSpec(Path("/System/Library/Fonts/STHeiti Light.ttc"), 0, "cjk", hans),
        _FontSpec(Path("/System/Library/Fonts/STHeiti Medium.ttc"), 0, "cjk", hans),
    )


@lru_cache(maxsize=1)
def _font_specs() -> tuple[_FontSpec, ...]:
    """探测实际存在的字体文件，只保留可用候选"""
    found = tuple(spec for spec in _font_candidates() if spec.path.is_file())
    cjk_count = sum(1 for spec in found if spec.kind == "cjk")
    if not cjk_count:
        logger.warning("未找到任何 CJK 字体，中文题面将回退 PIL 默认字体（CJK 会显示为方框）")
    if not any(spec.kind == "latin" for spec in found):
        logger.warning("未找到拉丁字体，纯拉丁题面（math/en）将回退 CJK 或 PIL 默认字体")
    logger.debug(f"文字题面图片可用字体 {len(found)} 个: {[(s.kind, str(s.path)) for s in found]}")
    return found


_CJK_RANGES = (
    (0x2E80, 0x9FFF),  # 部首扩展 + 假名 + CJK 统一表意
    (0xAC00, 0xD7AF),  # 谚文
    (0xF900, 0xFAFF),  # CJK 兼容表意
    (0xFF00, 0xFFEF),  # 全角/半角形式
)


def _has_cjk(text: str) -> bool:
    """文本是否含 CJK 字符（决定走 CJK 字体池还是拉丁池）"""
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text)


@lru_cache(maxsize=128)
def _load_font(path: str, index: int, size: int) -> ImageFont.FreeTypeFont:
    """按 (路径, ttc index, 像素字号) 缓存字体对象"""
    try:
        font = ImageFont.truetype(path, size=size, index=index)
        logger.debug(f"字体加载: {font.getname()} ({path}[{index}] @{size}px)")
        return font
    except OSError as exc:
        logger.warning(f"字体加载失败 {path}[{index}] @{size}px: {exc}，回退默认字体")
        # load_default(size) 运行时返回 FreeTypeFont（PIL 10+），stub 签名未区分
        return cast("ImageFont.FreeTypeFont", ImageFont.load_default(size))


def _tokenize(text: str) -> list[str]:
    """切成断行词元：英文单词保持完整，其余逐字符"""
    return _WORD_TOKEN_PATTERN.findall(text)


def _wrap_lines(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按像素宽度断行（词元级，CJK 逐字 / 英文按词）"""
    lines: list[str] = []
    current = ""
    for token in _tokenize(text):
        if not current and token.isspace():
            continue  # 行首不留空白
        candidate = current + token
        if current and font.getlength(candidate) > max_width:
            lines.append(current.rstrip())
            current = "" if token.isspace() else token
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _pick_font(
    text: str, locale: str, max_text_width: int
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """选出字号与排版：字号在「能排下的档位中偏大随机」，字体随机。

    字体池按文本内容选择：含 CJK 走 CJK 池（按 locale 过滤字形变体）；
    纯拉丁（math 表达式 / en 题面）优先拉丁池（DejaVu/Arial 对 ÷× 等符号
    字形完整），拉丁池为空时退 CJK 池再退 PIL 默认字体。
    字号不固定从最大档起步——短题面在最大两档间随机，长题面自动降档；
    最小字号仍排不下时接受任意行数（图片加高），生产健壮性优先。
    """
    if _has_cjk(text):
        specs = [spec for spec in _font_specs() if spec.kind == "cjk" and spec.supports(locale)]
    else:
        specs = [spec for spec in _font_specs() if spec.kind == "latin"]
        if not specs:
            # 无拉丁字体时用 CJK 池兜底（其拉丁与 ÷× 字形完整，仅风格不协调）
            specs = [spec for spec in _font_specs() if spec.kind == "cjk" and spec.supports(locale)]
    chosen = secrets.choice(specs) if specs else None

    def _font_for(logical_size: int) -> ImageFont.FreeTypeFont:
        if chosen is None:
            return cast("ImageFont.FreeTypeFont", ImageFont.load_default(logical_size * _SCALE))
        return _load_font(str(chosen.path), chosen.index, logical_size * _SCALE)

    fitted: list[tuple[ImageFont.FreeTypeFont, list[str]]] = []
    fallback: tuple[ImageFont.FreeTypeFont, list[str]] | None = None
    for logical_size in _FONT_SIZES:
        font = _font_for(logical_size)
        lines = _wrap_lines(text, font, max_text_width) or [text]
        if len(lines) <= 4:
            fitted.append((font, lines))
        elif fallback is None:
            # 第一个超限档位：保留为极端长文的兜底（后续档位只会更小）
            fallback = (font, lines)
            break

    if fitted:
        # 偏向大号：最大两档随机
        return secrets.choice(fitted[:2])
    assert fallback is not None
    return fallback


def _draw_noise(
    draw: ImageDraw.ImageDraw, width: int, height: int, color: tuple[int, int, int]
) -> None:
    """低对比度背景噪声：随机圆点与短线（不干扰文字阅读）"""
    count = max(16, width // (28 * _SCALE))
    for _ in range(count):
        x = secrets.randbelow(width)
        y = secrets.randbelow(height)
        radius = secrets.randbelow(3 * _SCALE) + 1
        if secrets.randbelow(2):
            draw.ellipse((x, y, x + radius, y + radius), fill=color)
        else:
            length = secrets.randbelow(12 * _SCALE) + 2
            draw.line((x, y, x + length, y), fill=color, width=_SCALE)


def _to_buffered_png(image: Image.Image, filename: str) -> BufferedInputFile:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return BufferedInputFile(output.getvalue(), filename=filename)


def render_text_image(text: str, *, locale: str = "zh-Hans") -> BufferedInputFile:
    """把题面文本渲染成随机化 PNG。

    Args:
        text: 题面纯文本（不含 HTML；HTML 转义仅适用于 caption，不适用图片）
        locale: 用于选择 CJK 字体变体（zh-Hans/zh-Hant）

    Returns:
        PNG 图片，文件名 verification-text.png

    Raises:
        ValueError: 题面为空或超过长度上限（catalog 内容受控，此为防御性校验）
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("文字题面不能为空")
    if len(stripped) > _MAX_TEXT_CHARS:
        raise ValueError(f"文字题面超过 {_MAX_TEXT_CHARS} 个字符上限")

    high_width = _LOGICAL_WIDTH * _SCALE
    padding = 34 * _SCALE
    max_text_width = high_width - padding * 2

    background, foreground, noise_color = secrets.choice(_PALETTES)
    font, lines = _pick_font(stripped, locale, max_text_width)

    # 每行独立图层：绘制后按随机角度微旋转（±2°），增强布局随机性
    line_gap = secrets.randbelow(7 * _SCALE) + 10 * _SCALE
    line_padding = 8 * _SCALE
    layers: list[Image.Image] = []
    for line in lines:
        visible = line or " "
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        bbox = probe.textbbox((0, 0), visible, font=font)
        line_width = max(20 * _SCALE, ceil(font.getlength(visible)) + line_padding * 2)
        line_height = max(1, ceil(bbox[3] - bbox[1])) + line_padding * 2
        layer = Image.new("RGBA", (line_width, line_height), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(
            (line_padding - bbox[0], line_padding - bbox[1]),
            visible,
            font=font,
            fill=(*foreground, 255),
        )
        angle = (secrets.randbelow(401) - 200) / 100.0
        layers.append(layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True))

    high_height = padding * 2 + sum(layer.height for layer in layers)
    high_height += line_gap * (len(layers) - 1)
    canvas = Image.new("RGBA", (high_width, high_height), (*background, 255))
    _draw_noise(ImageDraw.Draw(canvas), high_width, high_height, noise_color)

    cursor_y = padding
    for layer in layers:
        jitter = (secrets.randbelow(13) - 6) * _SCALE
        x = max(0, min(high_width - layer.width, (high_width - layer.width) // 2 + jitter))
        canvas.alpha_composite(layer, (x, cursor_y))
        cursor_y += layer.height + line_gap

    logical_height = max(96, ceil(high_height / _SCALE))
    final = canvas.convert("RGB").resize(
        (_LOGICAL_WIDTH, logical_height), resample=Image.Resampling.LANCZOS
    )
    return _to_buffered_png(final, "verification-text.png")


def render_slider_image(correct_position: int) -> BufferedInputFile:
    """渲染滑块验证图片：4 个圆角方格，绿色为正确位置。

    直接绘制几何图形而非渲染 emoji 字符（绕开彩色 emoji 字体依赖）。
    """
    if correct_position not in range(4):
        raise ValueError("滑块正确位置必须是 0-3")

    logical_width, logical_height = 640, 200
    width, height = logical_width * _SCALE, logical_height * _SCALE
    background, _foreground, noise_color = secrets.choice(_PALETTES)
    canvas = Image.new("RGBA", (width, height), (*background, 255))
    draw = ImageDraw.Draw(canvas)
    _draw_noise(draw, width, height, noise_color)

    margin = 36 * _SCALE
    gap = 16 * _SCALE
    tile_width = (width - margin * 2 - gap * 3) // 4
    tile_height = 120 * _SCALE
    top = (height - tile_height) // 2
    for index in range(4):
        left = margin + index * (tile_width + gap)
        if index == correct_position:
            fill, outline = (76, 168, 101, 255), (37, 108, 59, 255)
        else:
            fill, outline = (238, 242, 246, 255), (171, 181, 193, 255)
        draw.rounded_rectangle(
            (left, top, left + tile_width, top + tile_height),
            radius=18 * _SCALE,
            fill=fill,
            outline=outline,
            width=2 * _SCALE,
        )

    final = canvas.convert("RGB").resize(
        (logical_width, logical_height), resample=Image.Resampling.LANCZOS
    )
    return _to_buffered_png(final, "verification-slider.png")
