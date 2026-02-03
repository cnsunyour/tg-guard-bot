"""图片 OCR 模块 - 使用 EasyOCR 提取图片中的文字"""

from pathlib import Path

from loguru import logger

from src.core.utils import mask_text


class OCRExtractor:
    """图片文字提取器（使用 EasyOCR）"""

    def __init__(self):
        """初始化 OCR 提取器"""
        self._reader = None
        self._initialized = False

    def _init_ocr(self):
        """延迟初始化 EasyOCR（首次使用时才加载）"""
        if self._initialized:
            return

        # 检查是否启用 OCR 功能
        from src.core.config import settings

        if not settings.enable_ocr:
            logger.info("OCR 功能已禁用（ENABLE_OCR=False），跳过初始化")
            self._initialized = False
            return

        try:
            import easyocr

            logger.info("正在初始化 EasyOCR（首次使用会下载模型，约 500MB）...")

            # ✅ EasyOCR：基于 PyTorch，兼容所有 CPU，无 AVX2 要求
            self._reader = easyocr.Reader(
                ["ch_sim", "en"],  # 简体中文 + 英文
                gpu=False,  # 使用 CPU
                model_storage_directory=str(Path.home() / ".EasyOCR" / "model"),
                download_enabled=True,
                verbose=False,  # 禁用详细日志
            )

            self._initialized = True
            logger.info("✅ EasyOCR 初始化成功")

        except ImportError:
            logger.warning(
                "EasyOCR 未安装，OCR 功能不可用。安装命令: pip install easyocr torch torchvision"
            )
            self._initialized = False
        except Exception as e:
            logger.error(f"EasyOCR 初始化失败: {e}")
            logger.warning("OCR 功能不可用")
            self._initialized = False

    def extract_text(self, image_path: str) -> str | None:
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
            allowed_extensions = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
            if image_path_obj.suffix.lower() not in allowed_extensions:
                logger.warning(f"不支持的图片格式: {image_path_obj.suffix}")
                return None

            # 执行 OCR（EasyOCR）
            result = self._reader.readtext(str(image_path_obj))

            if not result:
                logger.debug(f"图片中未检测到文字: {image_path}")
                return None

            # 提取所有文本块
            texts = []
            for detection in result:
                # detection 格式: (bbox, text, confidence)
                _bbox, text, confidence = detection

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

    def extract_text_with_details(self, image_path: str) -> list[tuple[str, float]] | None:
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
            # ✅ M9: 验证路径安全性
            image_path_obj = Path(image_path).resolve()

            if not image_path_obj.exists():
                logger.error(f"图片文件不存在: {image_path}")
                return None

            # 检查是否是文件（不是目录）
            if not image_path_obj.is_file():
                logger.error(f"路径不是文件: {image_path}")
                return None

            # 检查文件扩展名
            allowed_extensions = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
            if image_path_obj.suffix.lower() not in allowed_extensions:
                logger.warning(f"不支持的图片格式: {image_path_obj.suffix}")
                return None

            # 执行 OCR（EasyOCR）
            result = self._reader.readtext(str(image_path_obj))

            if not result:
                return None

            # 提取文本和置信度
            texts_with_conf = []
            for detection in result:
                # detection 格式: (bbox, text, confidence)
                _bbox, text, confidence = detection

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
_extractor: OCRExtractor | None = None


def get_ocr_extractor() -> OCRExtractor:
    """获取全局 OCR 提取器实例"""
    global _extractor
    if _extractor is None:
        _extractor = OCRExtractor()
    return _extractor
