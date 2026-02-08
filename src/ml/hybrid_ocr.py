"""混合 OCR 服务（协调器）

支持多个 OCR 提供者的自动回退和熔断保护：
- OpenAI OCR（云 API，第一优先级）
- 百度智能云 OCR（云 API，第二优先级）
- PaddleOCR（本地，轻量级）
- EasyOCR（本地，最终回退）
"""

from collections import defaultdict
from datetime import datetime, timedelta

from loguru import logger

from src.core.config import settings
from src.ml.ocr_providers import (
    BaiduOCRProvider,
    EasyOCRProvider,
    OCRProvider,
    OCRProviderError,
    OpenAIOCRProvider,
    PaddleOCRProvider,
)


class HybridOCRService:
    """混合 OCR 服务（自动回退）

    支持多个 OCR 提供者的智能切换和熔断保护
    """

    def __init__(
        self,
        providers: list[OCRProvider] | None = None,
        enable_fallback: bool = True,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_minutes: int = 5,
    ):
        """初始化混合 OCR 服务

        Args:
            providers: OCR 提供者列表（按优先级排序）
            enable_fallback: 是否启用自动回退
            circuit_breaker_threshold: 熔断器阈值（连续失败次数）
            circuit_breaker_cooldown_minutes: 熔断器冷却时间（分钟）
        """
        self.providers = providers or []
        self.enable_fallback = enable_fallback
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_cooldown = timedelta(minutes=circuit_breaker_cooldown_minutes)
        self._stats: dict[str, dict] = defaultdict(
            lambda: {
                "success_count": 0,
                "failure_count": 0,
                "consecutive_failures": 0,
                "last_failure_time": None,
                "last_error": "",
            }
        )

        if not self.providers:
            # 从配置创建提供者
            self._init_providers_from_config()

    def _init_providers_from_config(self) -> None:
        """从配置创建 OCR 提供者"""
        # 1. OpenAI OCR（如果配置了，第一优先级）
        if settings.ocr_openai_enabled and settings.ocr_openai_api_key:
            try:
                self.providers.append(
                    OpenAIOCRProvider(
                        api_key=settings.ocr_openai_api_key,
                        model_name=settings.ocr_openai_model,
                        api_url=settings.ocr_openai_api_url or None,
                        timeout=settings.ocr_openai_timeout,
                    )
                )
                logger.info(
                    f"✅ OpenAI OCR 已启用 [模型:{settings.ocr_openai_model}]"
                    + (
                        f" [API URL:{settings.ocr_openai_api_url}]"
                        if settings.ocr_openai_api_url
                        else ""
                    )
                )
            except Exception as e:
                logger.warning(f"OpenAI OCR 初始化失败: {e}")

        # 2. 百度云 OCR（如果配置了，第二优先级）
        if settings.ocr_baidu_enabled and settings.ocr_baidu_api_key:
            try:
                self.providers.append(
                    BaiduOCRProvider(
                        api_key=settings.ocr_baidu_api_key,
                        secret_key=settings.ocr_baidu_secret_key,
                        use_accurate=settings.ocr_baidu_use_accurate,
                        timeout=settings.ocr_baidu_timeout,
                    )
                )
                logger.info(
                    f"✅ 百度云 OCR 已启用 [{'高精度' if settings.ocr_baidu_use_accurate else '标准'}]"
                )
            except Exception as e:
                logger.warning(f"百度云 OCR 初始化失败: {e}")

        # 3. PaddleOCR（如果启用）
        if settings.ocr_paddle_enabled:
            try:
                self.providers.append(PaddleOCRProvider(lang=settings.ocr_paddle_lang))
                logger.info(f"✅ PaddleOCR 已启用 [语言:{settings.ocr_paddle_lang}]")
            except Exception as e:
                logger.warning(f"PaddleOCR 初始化失败: {e}")

        # 4. EasyOCR（如果启用，最终回退）
        if settings.ocr_easy_enabled:
            try:
                self.providers.append(EasyOCRProvider())
                logger.info("✅ EasyOCR 已启用（最终回退）")
            except Exception as e:
                logger.warning(f"EasyOCR 初始化失败: {e}")

        if not self.providers:
            logger.warning("⚠️ 没有可用的 OCR 提供者")

    def _is_circuit_open(self, provider: OCRProvider) -> bool:
        """检查熔断器是否打开

        Args:
            provider: OCR 提供者

        Returns:
            True 如果熔断器打开（应跳过此提供者）
        """
        stats = self._stats[provider.name]

        # 检查连续失败次数
        if stats["consecutive_failures"] >= self.circuit_breaker_threshold:
            # 检查冷却时间
            if stats["last_failure_time"]:
                elapsed = datetime.now() - stats["last_failure_time"]
                if elapsed < self.circuit_breaker_cooldown:
                    logger.debug(
                        f"🔌 {provider.name} 熔断器打开，跳过 "
                        f"（剩余冷却时间: {self.circuit_breaker_cooldown - elapsed}）"
                    )
                    return True
                else:
                    # 冷却时间已过，重置熔断器
                    logger.debug(f"🔄 {provider.name} 熔断器冷却完成，重置")
                    stats["consecutive_failures"] = 0
                    stats["last_failure_time"] = None

        return False

    def _record_success(self, provider: OCRProvider) -> None:
        """记录成功

        Args:
            provider: OCR 提供者
        """
        stats = self._stats[provider.name]
        stats["success_count"] += 1
        stats["consecutive_failures"] = 0

    def _record_failure(self, provider: OCRProvider, error: str) -> None:
        """记录失败

        Args:
            provider: OCR 提供者
            error: 错误信息
        """
        stats = self._stats[provider.name]
        stats["failure_count"] += 1
        stats["consecutive_failures"] += 1
        stats["last_failure_time"] = datetime.now()
        stats["last_error"] = error

        # 检查是否触发熔断
        if stats["consecutive_failures"] >= self.circuit_breaker_threshold:
            logger.warning(
                f"⚠️ {provider.name} 触发熔断器 " f"（连续失败 {stats['consecutive_failures']} 次）"
            )

    async def extract_text(self, image_path: str) -> str | None:
        """尝试所有提供者，直到成功

        Args:
            image_path: 图片文件路径

        Returns:
            提取的文本，如果所有提供者都失败返回 None
        """
        last_error: Exception | None = None

        for provider in self.providers:
            # 检查熔断器
            if self._is_circuit_open(provider):
                continue

            # 尝试提取
            try:
                logger.debug(f"🔍 尝试使用 {provider.name} 提取文本...")
                result = await provider.extract(image_path)

                if result:
                    self._record_success(provider)
                    logger.info(
                        f"✅ {provider.name} 提取成功 [字数:{len(result.text)}] "
                        f"[成功率:{self._get_success_rate(provider.name):.1%}]"
                    )
                    return result.text
                else:
                    # 未检测到文本，不算失败
                    logger.debug(f"{provider.name}: 未检测到文本")
                    return None

            except OCRProviderError as e:
                last_error = e
                self._record_failure(provider, str(e))
                logger.warning(f"❌ {provider.name} 提取失败: {e}")

                if not self.enable_fallback:
                    # 不启用回退，直接抛出异常
                    raise
                else:
                    # 继续尝试下一个提供者
                    continue

            except Exception as e:
                last_error = e
                self._record_failure(provider, str(e))
                logger.error(f"💥 {provider.name} 发生意外错误: {e}")

                if not self.enable_fallback:
                    raise
                else:
                    continue

        # 所有提供者都失败
        if last_error:
            logger.error(f"🚨 所有 OCR 提供者都失败，最后错误: {last_error}")

        return None

    async def extract_text_with_details(self, image_path: str) -> list[tuple[str, float]] | None:
        """尝试所有提供者，直到成功（带详细信息）

        Args:
            image_path: 图片文件路径

        Returns:
            [(文本, 置信度), ...] 列表，如果所有提供者都失败返回 None
        """
        last_error: Exception | None = None

        for provider in self.providers:
            # 检查熔断器
            if self._is_circuit_open(provider):
                continue

            # 尝试提取
            try:
                logger.debug(f"🔍 尝试使用 {provider.name} 提取文本（详细信息）...")
                result = await provider.extract_with_details(image_path)

                if result:
                    self._record_success(provider)
                    logger.info(
                        f"✅ {provider.name} 提取成功 [块数:{len(result)}] "
                        f"[成功率:{self._get_success_rate(provider.name):.1%}]"
                    )
                    return result
                else:
                    # 未检测到文本，不算失败
                    logger.debug(f"{provider.name}: 未检测到文本")
                    return None

            except OCRProviderError as e:
                last_error = e
                self._record_failure(provider, str(e))
                logger.warning(f"❌ {provider.name} 提取失败: {e}")

                if not self.enable_fallback:
                    raise
                else:
                    continue

            except Exception as e:
                last_error = e
                self._record_failure(provider, str(e))
                logger.error(f"💥 {provider.name} 发生意外错误: {e}")

                if not self.enable_fallback:
                    raise
                else:
                    continue

        # 所有提供者都失败
        if last_error:
            logger.error(f"🚨 所有 OCR 提供者都失败，最后错误: {last_error}")

        return None

    def _get_success_rate(self, provider_name: str) -> float:
        """计算提供者成功率

        Args:
            provider_name: 提供者名称

        Returns:
            成功率（0.0-1.0）
        """
        stats = self._stats[provider_name]
        total = stats["success_count"] + stats["failure_count"]
        if total == 0:
            return 0.0
        return stats["success_count"] / total

    def get_stats(self) -> dict[str, dict]:
        """获取所有提供者的统计信息

        Returns:
            {provider_name: stats} 字典
        """
        return dict(self._stats)

    def reset_stats(self, provider_name: str | None = None) -> None:
        """重置统计信息

        Args:
            provider_name: 提供者名称，None 表示重置所有
        """
        if provider_name:
            if provider_name in self._stats:
                self._stats[provider_name] = {
                    "success_count": 0,
                    "failure_count": 0,
                    "consecutive_failures": 0,
                    "last_failure_time": None,
                    "last_error": "",
                }
                logger.debug(f"🔄 {provider_name} 统计信息已重置")
        else:
            self._stats.clear()
            logger.debug("🔄 所有提供者统计信息已重置")


# ============================================================================
# 全局实例（单例模式）
# ============================================================================

_hybrid_ocr: HybridOCRService | None = None


def get_hybrid_ocr() -> HybridOCRService:
    """获取混合 OCR 服务全局实例（单例模式）

    Returns:
        HybridOCRService 实例
    """
    global _hybrid_ocr
    if _hybrid_ocr is None:
        _hybrid_ocr = HybridOCRService()
    return _hybrid_ocr


def reset_hybrid_ocr() -> None:
    """重置全局混合 OCR 服务实例

    主要用于测试或重新配置
    """
    global _hybrid_ocr
    _hybrid_ocr = None
    logger.debug("🔄 全局混合 OCR 服务已重置")
