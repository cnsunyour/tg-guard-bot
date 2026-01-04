"""图片 OCR 模块 - 使用 PaddleOCR 提取图片中的文字"""

from typing import Optional, List, Tuple
from pathlib import Path
from loguru import logger

from src.core.utils import mask_text


class OCRExtractor:
    """图片文字提取器（使用 PaddleOCR）"""

    def __init__(self):
        """初始化 OCR 提取器"""
        self._ocr = None
        self._initialized = False

    def _init_ocr(self):
        """延迟初始化 PaddleOCR（首次使用时才加载）"""
        if self._initialized:
            return

        try:
            from paddleocr import PaddleOCR

            # 初始化 PaddleOCR
            # 参考文档: https://www.paddleocr.ai/
            # 新版本已移除 use_gpu 和 show_log 参数
            self._ocr = PaddleOCR(
                use_angle_cls=True,     # 启用文字方向分类器，提高倾斜文字识别准确度
                lang="ch",              # 中文+英文模型
                det=True,               # 启用文本检测
                rec=True,               # 启用文本识别
                drop_score=0.6,         # 置信度阈值，过滤低质量结果（提高到 0.6）
            )

            self._initialized = True
            logger.info("PaddleOCR 初始化成功")

        except ImportError:
            logger.warning(
                "PaddleOCR 未安装，OCR 功能不可用。"
                "安装命令: pip install paddleocr paddlepaddle"
            )
            self._initialized = False
        except Exception as e:
            logger.error(f"PaddleOCR 初始化失败: {e}")
            self._initialized = False

    def extract_text(self, image_path: str) -> Optional[str]:
        """从图片中提取文字

        Args:
            image_path: 图片文件路径

        Returns:
            提取的文字（所有文本块拼接），如果失败返回 None
        """
        if not self._initialized:
            self._init_ocr()

        if not self._initialized:
            return None

        try:
            # ✅ M9: 验证路径安全性
            image_path_obj = Path(image_path).resolve()

            # 检查文件是否存在
            if not image_path_obj.exists():
                logger.error(f"图片文件不存在: {image_path}")
                return None

            # 检查是否是文件（不是目录）
            if not image_path_obj.is_file():
                logger.error(f"路径不是文件: {image_path}")
                return None

            # 检查文件扩展名是否为图片格式
            allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
            if image_path_obj.suffix.lower() not in allowed_extensions:
                logger.warning(f"不支持的图片格式: {image_path_obj.suffix}")
                return None

            # 执行 OCR
            result = self._ocr.ocr(str(image_path_obj), cls=True)

            if not result or not result[0]:
                logger.debug(f"图片中未检测到文字: {image_path}")
                return None

            # 提取所有文本块
            texts = []
            for line in result[0]:
                # line 格式: [[[x1,y1], [x2,y2], [x3,y3], [x4,y4]], (text, confidence)]
                if len(line) >= 2:
                    text = line[1][0]  # 获取识别的文字
                    confidence = line[1][1]  # 获取置信度

                    # 过滤低置信度结果
                    if confidence > 0.6:
                        texts.append(text)

            if not texts:
                logger.debug(f"图片中未提取到有效文字: {image_path}")
                return None

            # 拼接所有文本
            full_text = " ".join(texts)
            logger.debug(f"从图片提取文字 ({len(texts)} 块): {mask_text(full_text)}")

            return full_text

        except Exception as e:
            logger.error(f"OCR 提取失败: {e}")
            return None

    def extract_text_with_details(
        self, image_path: str
    ) -> Optional[List[Tuple[str, float]]]:
        """从图片中提取文字（带详细信息）

        Args:
            image_path: 图片文件路径

        Returns:
            [(文字, 置信度), ...] 列表，如果失败返回 None
        """
        if not self._initialized:
            self._init_ocr()

        if not self._initialized:
            return None

        try:
            if not Path(image_path).exists():
                logger.error(f"图片文件不存在: {image_path}")
                return None

            result = self._ocr.ocr(image_path, cls=True)

            if not result or not result[0]:
                return None

            # 提取文本和置信度
            texts_with_conf = []
            for line in result[0]:
                if len(line) >= 2:
                    text = line[1][0]
                    confidence = line[1][1]

                    if confidence > 0.6:
                        texts_with_conf.append((text, confidence))

            return texts_with_conf if texts_with_conf else None

        except Exception as e:
            logger.error(f"OCR 提取失败: {e}")
            return None

    @property
    def is_available(self) -> bool:
        """OCR 是否可用"""
        if not self._initialized:
            self._init_ocr()
        return self._initialized


# 全局 OCR 提取器实例
_extractor: Optional[OCRExtractor] = None


def get_ocr_extractor() -> OCRExtractor:
    """获取全局 OCR 提取器实例"""
    global _extractor
    if _extractor is None:
        _extractor = OCRExtractor()
    return _extractor
