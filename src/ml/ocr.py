"""图片 OCR 模块 - 使用混合 OCR 服务提取图片中的文字

支持多种 OCR 提供者：
- Gemini AI OCR（云 API，第一优先级）
- 百度智能云 OCR（云 API，第二优先级）
- PaddleOCR（本地，轻量级）
- EasyOCR（本地，最终回退）
"""

import asyncio

from loguru import logger

from src.core.utils import mask_text


class OCRExtractor:
    """图片文字提取器（使用混合 OCR 服务）

    适配器模式，保持向后兼容
    """

    def __init__(self):
        """初始化 OCR 提取器"""
        self._hybrid_ocr = None
        self._initialized = False

    def _init_ocr(self):
        """延迟初始化混合 OCR 服务"""
        if self._initialized:
            return

        # 检查是否启用 OCR 功能
        from src.core.config import settings

        if not settings.enable_ocr:
            logger.info("OCR 功能已禁用（ENABLE_OCR=False），跳过初始化")
            self._initialized = False
            return

        try:
            from src.ml.hybrid_ocr import get_hybrid_ocr

            self._hybrid_ocr = get_hybrid_ocr()
            self._initialized = True

            if self._hybrid_ocr.providers:
                logger.info(
                    f"✅ OCR 提取器初始化成功（混合模式） "
                    f"[提供者数量:{len(self._hybrid_ocr.providers)}]"
                )
            else:
                logger.warning("⚠️ OCR 提取器初始化完成，但没有可用的提供者")
                self._initialized = False

        except Exception as e:
            logger.error(f"OCR 提取器初始化失败: {e}")
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

        if not self._initialized or not self._hybrid_ocr:
            return None

        try:
            # 检查是否有运行的事件循环
            try:
                asyncio.get_running_loop()
                # 在异步上下文中，无法等待异步任务
                # 这种情况下，建议调用方直接使用异步方法
                logger.warning(
                    "⚠️ 在异步上下文中调用了同步的 extract_text()，"
                    "建议使用异步方法或在新线程中运行"
                )
                return None
            except RuntimeError:
                # 在同步上下文中，创建新的事件循环
                result = asyncio.run(self._hybrid_ocr.extract_text(image_path))

                if result:
                    logger.debug(f"从图片提取文字: {mask_text(result)}")

                return result

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

        if not self._initialized or not self._hybrid_ocr:
            return None

        try:
            # 检查是否有运行的事件循环
            try:
                asyncio.get_running_loop()
                # 在异步上下文中，无法等待异步任务
                logger.warning(
                    "⚠️ 在异步上下文中调用了同步的 extract_text_with_details()，"
                    "建议使用异步方法或在新线程中运行"
                )
                return None
            except RuntimeError:
                # 在同步上下文中，创建新的事件循环
                result = asyncio.run(self._hybrid_ocr.extract_text_with_details(image_path))

                return result

        except Exception as e:
            logger.error(f"OCR 提取失败: {e}")
            return None

    @property
    def is_available(self) -> bool:
        """OCR 是否可用"""
        if not self._initialized:
            self._init_ocr()
        return self._initialized and self._hybrid_ocr is not None


# 全局 OCR 提取器实例
_extractor: OCRExtractor | None = None


def get_ocr_extractor() -> OCRExtractor:
    """获取全局 OCR 提取器实例"""
    global _extractor
    if _extractor is None:
        _extractor = OCRExtractor()
    return _extractor
