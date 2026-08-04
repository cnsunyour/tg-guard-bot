"""反垃圾检测服务 - 整合三阶段管道

✅ P1-11: CPU 密集型操作已移至线程池
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict

from loguru import logger

from src.core.config import settings
from src.core.executor import run_in_executor  # ✅ P1-11: 导入线程池执行器
from src.core.utils import mask_text
from src.ml.ai_detector import (
    VisionAllFailedError,
    VisionUnsupportedError,
    _read_image_as_base64,
    get_ai_detector,
)
from src.ml.classifier import get_classifier
from src.ml.embedder import get_embedder
from src.ml.rule_engine import get_rule_engine
from src.repositories.spam_repo import SpamRepository


class DetectionResult(TypedDict):
    """检测结果类型"""

    is_spam: bool
    confidence: float
    original_confidence: float  # 原始置信度
    activity_reduction: float  # 活跃度减少值
    stage: str | None
    reasons: list[str]
    details: dict[str, Any]


class RetrainCode(StrEnum):
    """retrain_model 稳定结果 code(handler 据 code 选 catalog key 渲染)。"""

    insufficient_samples = "insufficient_samples"
    save_failed = "save_failed"
    success = "success"
    failed = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrainResult:
    """retrain_model 结果:稳定 code + 渲染参数(不含中文文案)。

    success 由 code 计算,避免 success=True/code=failed 非法组合。
    """

    code: RetrainCode
    params: Mapping[str, str | int | float]

    @property
    def success(self) -> bool:
        return self.code is RetrainCode.success


# 最少训练样本数(独立常量,供结果 params 与校验共用)
_MIN_TRAINING_SAMPLES = 10


class SpamDetector:
    """反垃圾检测服务 - 三阶段管道"""

    def __init__(self):
        """初始化检测器"""
        self.rule_engine = get_rule_engine()
        self.classifier = get_classifier()
        self.embedder = get_embedder()
        self.ai_detector = get_ai_detector()

    async def detect(
        self, text: str, user_id: int, chat_id: int, activity: int | None = None
    ) -> DetectionResult:
        """检测文本是否为垃圾信息

        Args:
            text: 待检测文本
            user_id: 用户 ID
            chat_id: 群组 ID
            activity: 用户活跃度（可选），用于置信度调整

        Returns:
            检测结果字典
        """
        result: DetectionResult = {
            "is_spam": False,
            "confidence": 0.0,
            "original_confidence": 0.0,  # 原始置信度
            "activity_reduction": 0.0,  # 活跃度减少值
            "stage": None,
            "reasons": [],
            "details": {},
        }

        # ========== 两阶段预过滤 ==========
        # 阶段1: 先运行规则引擎快速检测（所有消息都检测）
        rule_result = await run_in_executor(self.rule_engine.analyze, text)

        # 如果规则引擎检测到垃圾，继续完整检测流程
        # （即使是短消息，也会被检测到）
        if rule_result["is_spam"]:
            logger.debug(
                f"规则引擎检测到垃圾特征 [用户:{user_id}] "
                f"[置信度:{rule_result['confidence']:.2f}] "
                f"[原因:{rule_result['reasons']}]"
            )
            # 继续执行后续检测流程...
        else:
            # 阶段2: 规则引擎未检测到垃圾，检查文本长度
            min_length = settings.spam_min_text_length
            if min_length > 0:
                from src.core.utils import calculate_normalized_length

                text_length = calculate_normalized_length(text)
                if text_length < min_length:
                    logger.debug(
                        f"跳过过短消息检测 [群组:{chat_id}] [用户:{user_id}] "
                        f"[标准化长度:{text_length}] [阈值:{min_length}] [原始长度:{len(text)}]"
                    )
                    result["stage"] = "skipped_short"
                    result["details"] = {
                        "normalized_length": text_length,
                        "original_length": len(text),
                        "min_length": min_length,
                    }
                    return result

        # Stage 1: 规则引擎（快速过滤）
        # ✅ P1-11: 在线程池中运行，避免阻塞事件循环
        # 注意：rule_result 已在两阶段预过滤中计算过，这里直接使用

        if rule_result["is_spam"]:
            result["is_spam"] = True
            result["confidence"] = rule_result["confidence"]
            result["stage"] = "rule_engine"
            result["reasons"] = rule_result["reasons"]
            result["details"] = rule_result["details"]

            logger.info(
                f"Stage 1 检测到垃圾信息 [用户:{user_id}] 原因: {', '.join(rule_result['reasons'])}"
            )

            # 应用活跃度置信度调整
            result = self._apply_activity_adjustment(result, activity, user_id)
            return result

        # Stage 2: ML 分类器（捕获变体）
        if self.classifier.is_trained:
            try:
                # ✅ P1-11: 在线程池中运行 ML 推理
                is_spam_ml, confidence_ml = await run_in_executor(self.classifier.predict, text)

                if is_spam_ml and confidence_ml > settings.spam_threshold_ml:
                    result["is_spam"] = True
                    result["confidence"] = confidence_ml
                    result["stage"] = "ml_classifier"
                    result["reasons"].append(f"ML 分类器 (置信度: {confidence_ml:.2f})")

                    logger.info(
                        f"Stage 2 检测到垃圾信息 [用户:{user_id}] 置信度: {confidence_ml:.2f}"
                    )

                    # 应用活跃度置信度调整
                    result = self._apply_activity_adjustment(result, activity, user_id)
                    return result

            except Exception as e:
                logger.error(f"ML 分类器检测失败: {e}")

        # Stage 3: Embedding 语义分析（处理边界情况）
        if self.embedder.is_initialized:
            try:
                # ✅ P1-11: 在线程池中运行 Embedding 推理
                is_spam_emb, similarity = await run_in_executor(self.embedder.predict, text)

                if is_spam_emb:
                    result["is_spam"] = True
                    result["confidence"] = similarity
                    result["stage"] = "embedding"
                    result["reasons"].append(f"语义相似度 ({similarity:.2f})")

                    logger.info(f"Stage 3 检测到垃圾信息 [用户:{user_id}] 相似度: {similarity:.2f}")

                    # 应用活跃度置信度调整
                    result = self._apply_activity_adjustment(result, activity, user_id)
                    return result

            except Exception as e:
                logger.error(f"Embedding 检测失败: {e}")

        # 未检测到垃圾信息
        logger.debug(f"消息通过检测 [用户:{user_id}]")
        return result

    async def detect_with_ai(
        self,
        text: str,
        user_id: int,
        chat_id: int,
        activity: int | None = None,
        skip_auto_train: bool = False,
    ) -> DetectionResult:
        """并行检测入口 - 同时运行传统三阶段管道和 AI API 检测

        Args:
            text: 待检测文本
            user_id: 用户 ID
            chat_id: 群组 ID
            activity: 用户活跃度（可选）
            skip_auto_train: 是否跳过 AI 自动入库（确认模式下使用）

        Returns:
            合并后的检测结果字典
        """
        # 如果 AI 检测未启用，直接使用传统三阶段
        if not self.ai_detector.enabled:
            return await self.detect(text, user_id, chat_id, activity)

        # 对短文本保持与传统预过滤一致，避免无谓 AI 调用
        min_length = settings.spam_min_text_length
        if min_length > 0:
            from src.core.utils import calculate_normalized_length

            if calculate_normalized_length(text) < min_length:
                return await self.detect(text, user_id, chat_id, activity)

        # 并行执行传统检测和 AI 检测
        try:
            results = await asyncio.gather(
                self.detect(text, user_id, chat_id, activity),  # 传统三阶段
                self.ai_detector.detect(text),  # AI API 检测
                return_exceptions=True,
            )

            traditional_result = results[0]
            ai_result = results[1]

            # 检查是否有异常
            if isinstance(traditional_result, Exception):
                logger.error(f"传统检测失败: {traditional_result}")
                traditional_result = None

            if isinstance(ai_result, Exception):
                logger.error(f"AI 检测失败: {ai_result}")
                ai_result = None

            # 合并结果
            return await self._merge_detection_results(
                traditional_result, ai_result, text, user_id, activity, skip_auto_train
            )

        except Exception as e:
            logger.error(f"并行检测失败: {e}")
            # 降级到传统检测
            return await self.detect(text, user_id, chat_id, activity)

    async def detect_with_ai_context(
        self,
        text: str,
        user_id: int,
        chat_id: int,
        activity: int | None = None,
        context_text: str | None = None,
        context_messages: list[dict] | None = None,
        message: Any = None,
        skip_auto_train: bool = False,
    ) -> DetectionResult:
        """带上下文的并行检测入口 - 同时运行传统三阶段管道和 AI 上下文检测

        Args:
            text: 待检测文本
            user_id: 用户 ID
            chat_id: 群组 ID
            activity: 用户活跃度（可选）
            context_text: 上下文文本（格式化后的对话上下文，给 AI 用）
            context_messages: 原始上下文消息列表（给 Embedding 用）
            message: Telegram Message 对象（用于回复链检测）
            skip_auto_train: 是否跳过 AI 自动入库（确认模式下使用）

        Returns:
            合并后的检测结果字典
        """
        # 如果 AI 检测未启用，直接使用传统三阶段
        if not self.ai_detector.enabled:
            result = await self.detect(text, user_id, chat_id, activity)
            # 应用上下文调整（即使没有 AI 也可以用 Embedding）
            if settings.context_consistency_enabled and context_messages:
                result = await self._apply_context_adjustment(
                    result, text, message, context_messages, user_id
                )
            return result

        # 对短文本保持与传统预过滤一致，避免无谓 AI 调用
        min_length = settings.spam_min_text_length
        if min_length > 0:
            from src.core.utils import calculate_normalized_length

            if calculate_normalized_length(text) < min_length:
                result = await self.detect(text, user_id, chat_id, activity)
                if settings.context_consistency_enabled and context_messages:
                    result = await self._apply_context_adjustment(
                        result, text, message, context_messages, user_id
                    )
                return result

        # 并行执行传统检测和 AI 上下文检测
        try:
            results = await asyncio.gather(
                self.detect(text, user_id, chat_id, activity),  # 传统三阶段
                self.ai_detector.detect_with_context(text, context_text),  # AI 上下文检测
                return_exceptions=True,
            )

            traditional_result = results[0]
            ai_result = results[1]

            # 检查是否有异常
            if isinstance(traditional_result, Exception):
                logger.error(f"传统检测失败: {traditional_result}")
                traditional_result = None

            if isinstance(ai_result, Exception):
                logger.error(f"AI 上下文检测失败: {ai_result}")
                ai_result = None

            # 合并结果
            merged_result = await self._merge_detection_results(
                traditional_result, ai_result, text, user_id, activity, skip_auto_train
            )

            # 应用上下文调整（降低误判）
            if settings.context_consistency_enabled and context_messages:
                merged_result = await self._apply_context_adjustment(
                    merged_result, text, message, context_messages, user_id
                )

            return merged_result

        except Exception as e:
            logger.error(f"并行上下文检测失败: {e}")
            # 降级到传统检测
            result = await self.detect(text, user_id, chat_id, activity)
            # 应用上下文调整
            if settings.context_consistency_enabled and context_messages:
                result = await self._apply_context_adjustment(
                    result, text, message, context_messages, user_id
                )
            return result

    async def _merge_detection_results(
        self,
        traditional: DetectionResult | None,
        ai: dict[str, Any] | None,
        text: str,
        user_id: int,
        activity: int | None = None,
        skip_auto_train: bool = False,
    ) -> DetectionResult:
        """合并传统检测和 AI 检测结果

        合并策略：
        1. 传统检测为垃圾 → 使用传统结果
        2. AI 检测为垃圾 → 使用 AI 结果 + 自动入库训练
        3. 都不是垃圾 → 使用传统结果（置信度 0.0）
        4. 任一检测失败 → 使用另一个结果

        Args:
            traditional: 传统检测结果
            ai: AI 检测结果
            text: 原始文本
            user_id: 用户 ID
            activity: 用户活跃度

        Returns:
            合并后的检测结果
        """
        # 两者都失败 → 默认不是垃圾
        if traditional is None and ai is None:
            logger.warning(f"传统和 AI 检测都失败 [用户:{user_id}]")
            return {
                "is_spam": False,
                "confidence": 0.0,
                "original_confidence": 0.0,
                "activity_reduction": 0.0,
                "stage": "failed",
                "reasons": ["所有检测器都失败"],
                "details": {},
            }

        # 传统检测失败 → 使用 AI 结果
        if traditional is None:
            logger.warning(f"传统检测失败，使用 AI 结果 [用户:{user_id}]")
            assert ai is not None  # 类型检查
            if ai["is_spam"]:
                # ✅ 确认模式下跳过 AI 自动入库，等待管理员确认
                if not skip_auto_train:
                    await self._handle_ai_spam_detection(text, ai, user_id)
                else:
                    logger.debug(f"确认模式：跳过 AI 自动入库 [用户:{user_id}]")
            else:
                # 处理高置信度负样本
                if not skip_auto_train:
                    await self._handle_ai_negative_detection(text, ai, user_id)
            # AI 结果需要转换为 DetectionResult 格式 + 应用活跃度调整
            ai_result: DetectionResult = {
                "is_spam": ai.get("is_spam", False),
                "confidence": ai.get("confidence", 0.0),
                "original_confidence": ai.get("original_confidence", 0.0),
                "activity_reduction": ai.get("activity_reduction", 0.0),
                "stage": ai.get("stage"),
                "reasons": ai.get("reasons", []),
                "details": ai.get("details", {}),
            }
            # ✅ 应用活跃度调整
            return self._apply_activity_adjustment(ai_result, activity, user_id)

        # AI 检测失败 → 使用传统结果
        if ai is None:
            logger.warning(f"AI 检测失败，使用传统结果 [用户:{user_id}]")
            return traditional

        # 策略 1: 传统检测为垃圾 → 使用传统结果（优先级高）
        if traditional["is_spam"]:
            logger.info(
                f"传统检测为垃圾 [用户:{user_id}] [阶段:{traditional['stage']}] "
                f"[置信度:{traditional['confidence']:.2f}]"
            )
            return traditional

        # 策略 2: AI 检测为垃圾 → 使用 AI 结果 + 自动入库训练
        if ai["is_spam"]:
            logger.info(f"AI 检测为垃圾 [用户:{user_id}] [置信度:{ai['confidence']:.2f}]")
            # ✅ 确认模式下跳过 AI 自动入库，等待管理员确认
            if not skip_auto_train:
                await self._handle_ai_spam_detection(text, ai, user_id)
            else:
                logger.debug(f"确认模式：跳过 AI 自动入库 [用户:{user_id}]")
            # AI 结果需要转换为 DetectionResult 格式 + 应用活跃度调整
            converted_result: DetectionResult = {
                "is_spam": ai.get("is_spam", False),
                "confidence": ai.get("confidence", 0.0),
                "original_confidence": ai.get("original_confidence", 0.0),
                "activity_reduction": ai.get("activity_reduction", 0.0),
                "stage": ai.get("stage"),
                "reasons": ai.get("reasons", []),
                "details": ai.get("details", {}),
            }
            # ✅ 应用活跃度调整
            return self._apply_activity_adjustment(converted_result, activity, user_id)

        # 策略 3: 都不是垃圾 → 使用传统结果 + 检查是否入库负样本
        logger.debug(f"传统和 AI 都认为不是垃圾 [用户:{user_id}]")
        # 处理高置信度负样本
        if not skip_auto_train:
            await self._handle_ai_negative_detection(text, ai, user_id)
        return traditional

    async def _handle_ai_spam_detection(
        self, text: str, ai_result: dict[str, Any], user_id: int
    ) -> None:
        """处理 AI 检测到的垃圾信息

        - 自动入库作为训练样本（labeled_by = -1 标记为 AI 标注）
        - 检查是否触发自动训练

        Args:
            text: 原始文本
            ai_result: AI 检测结果
            user_id: 用户 ID
        """
        # 检查是否启用自动训练
        if not settings.ai_spam_auto_train:
            logger.debug("AI 自动训练未启用，跳过入库")
            return

        try:
            # 入库训练样本（labeled_by = -1 表示 AI 标注）
            success = await self.add_feedback(
                text=text,
                is_spam=True,
                labeled_by=settings.ai_spam_labeled_by,  # -1
                confidence=ai_result.get("confidence"),
            )

            if success:
                logger.info(
                    f"AI 检测样本已入库 [用户:{user_id}] "
                    f"[置信度:{ai_result.get('confidence', 0.0):.2f}] "
                    f"[labeled_by:{settings.ai_spam_labeled_by}]"
                )

                # 检查是否触发自动训练
                train_result = await self.check_and_auto_train(admin_ids=settings.admin_ids)

                if train_result is not None:
                    logger.info(f"AI 样本触发自动训练 [结果:{train_result.code.value}]")

        except Exception as e:
            logger.error(f"AI 样本入库失败 [用户:{user_id}]: {e}")

    async def _handle_ai_negative_detection(
        self, text: str, ai_result: dict[str, Any], user_id: int
    ) -> None:
        """处理 AI 检测到的高置信度负样本（正常消息）

        - 只在置信度 <= negative_threshold 时入库
        - 表示 AI 高度确信这不是垃圾信息
        - 作为高质量负样本用于训练

        Args:
            text: 原始文本
            ai_result: AI 检测结果
            user_id: 用户 ID
        """
        # 检查是否启用自动训练和负样本入库
        if not settings.ai_spam_auto_train:
            logger.debug("AI 自动训练未启用，跳过入库")
            return

        if not settings.ai_spam_auto_train_negatives:
            logger.debug("AI 负样本自动训练未启用，跳过入库")
            return

        # 检查置信度是否足够低（高度确信不是垃圾）
        confidence = ai_result.get("confidence", 1.0)
        if confidence > settings.ai_spam_negative_threshold:
            logger.debug(
                f"AI 负样本置信度过高，跳过入库 [用户:{user_id}] "
                f"[置信度:{confidence:.2f}] [阈值:{settings.ai_spam_negative_threshold}]"
            )
            return

        # ✅ 过滤条件 1: 必须包含中文
        if not any("\u4e00" <= char <= "\u9fff" for char in text):
            logger.debug(f"AI 负样本不包含中文，跳过入库 [用户:{user_id}] [文本:{text[:50]}]")
            return

        # ✅ 过滤条件 2: 长度必须超过 10 个字符
        if len(text) <= 10:
            logger.debug(f"AI 负样本长度过短，跳过入库 [用户:{user_id}] [长度:{len(text)}]")
            return

        try:
            # 入库负样本（labeled_by = -1 表示 AI 标注）
            success = await self.add_feedback(
                text=text,
                is_spam=False,  # 负样本
                labeled_by=settings.ai_spam_labeled_by,  # -1
                confidence=confidence,
            )

            if success:
                logger.info(
                    f"AI 负样本已入库 [用户:{user_id}] "
                    f"[置信度:{confidence:.2f}] "
                    f"[阈值:{settings.ai_spam_negative_threshold}] "
                    f"[labeled_by:{settings.ai_spam_labeled_by}]"
                )

                # 检查是否触发自动训练
                train_result = await self.check_and_auto_train(admin_ids=settings.admin_ids)

                if train_result is not None:
                    logger.info(f"AI 负样本触发自动训练 [结果:{train_result.code.value}]")

        except Exception as e:
            logger.error(f"AI 负样本入库失败 [用户:{user_id}]: {e}")

    def _apply_activity_adjustment(
        self, result: DetectionResult, activity: int | None, user_id: int
    ) -> DetectionResult:
        """应用活跃度置信度调整

        Args:
            result: 检测结果字典
            activity: 用户活跃度
            user_id: 用户 ID

        Returns:
            调整后的检测结果
        """
        # 如果未提供活跃度或活跃度过低，不调整
        if activity is None or activity < 10:
            return result

        # 如果未检测到垃圾，不调整
        if not result["is_spam"]:
            return result

        # 导入 ActivityService（延迟导入避免循环依赖）
        from src.services.activity import ActivityService

        # 保存原始置信度
        original_confidence = result["confidence"]
        result["original_confidence"] = original_confidence

        # 计算置信度减少值
        reduction = ActivityService.calculate_confidence_reduction(activity)
        result["activity_reduction"] = reduction

        # 应用调整
        adjusted_confidence = max(0.0, original_confidence - reduction)
        result["confidence"] = adjusted_confidence

        # 根据调整后的置信度重新评估是否为垃圾
        # 使用 ML 阈值作为参考（规则引擎通常置信度更高，不太可能被调整）
        threshold = settings.spam_threshold_ml

        if adjusted_confidence < threshold:
            result["is_spam"] = False
            logger.info(
                f"活跃度置信度调整 [用户:{user_id}] [活跃度:{activity}] "
                f"{original_confidence:.2f} -> {adjusted_confidence:.2f} (减少 {reduction:.2f}), "
                f"不再判定为垃圾"
            )
        else:
            logger.debug(
                f"活跃度置信度调整 [用户:{user_id}] [活跃度:{activity}] "
                f"{original_confidence:.2f} -> {adjusted_confidence:.2f} (减少 {reduction:.2f}), "
                f"仍判定为垃圾"
            )

        return result

    async def _apply_context_adjustment(
        self,
        result: DetectionResult,
        text: str,
        message: Any,
        context_messages: list[dict],
        user_id: int,
    ) -> DetectionResult:
        """应用上下文调整（只降低误判，不提高检测）

        Args:
            result: 检测结果字典
            text: 当前消息文本
            message: Telegram Message 对象
            context_messages: 上下文消息列表
            user_id: 用户 ID

        Returns:
            调整后的检测结果
        """
        # 如果不是垃圾，不需要调整
        if not result["is_spam"]:
            return result

        # 如果 Embedder 未初始化，无法进行语义分析
        if not self.embedder.is_initialized:
            logger.debug("Embedder 未初始化，跳过上下文调整")
            return result

        confidence_reduction = 0.0
        reasons = []

        try:
            # 1. 检查回复链相关性（优先级最高）
            if message and hasattr(message, "reply_to_message") and message.reply_to_message:
                reply_msg = message.reply_to_message
                if hasattr(reply_msg, "text") and reply_msg.text:
                    reply_similarity = await self.embedder.compute_similarity(text, reply_msg.text)

                    if reply_similarity >= settings.reply_similarity_threshold:
                        confidence_reduction += settings.reply_confidence_reduction
                        reasons.append(f"回复内容相关(相似度{reply_similarity:.2f})")
                        logger.info(
                            f"检测到回复链相关性 [用户:{user_id}] "
                            f"相似度={reply_similarity:.2f}, "
                            f"降低置信度 -{settings.reply_confidence_reduction}"
                        )

            # 2. 检查群组上下文一致性
            if context_messages and len(context_messages) >= 3:
                is_consistent, similarity = await self.embedder.detect_context_consistency(
                    text, context_messages
                )

                if is_consistent:
                    confidence_reduction += settings.context_confidence_reduction
                    reasons.append(f"与群组话题一致(相似度{similarity:.2f})")
                    logger.info(
                        f"检测到上下文一致性 [用户:{user_id}] "
                        f"相似度={similarity:.2f}, "
                        f"降低置信度 -{settings.context_confidence_reduction}"
                    )

            # 3. 应用调整
            if confidence_reduction > 0:
                original_confidence = result["confidence"]
                adjusted_confidence = max(0.0, original_confidence - confidence_reduction)
                result["confidence"] = adjusted_confidence
                result["reasons"].extend(reasons)

                # 如果置信度降到阈值以下，改为非垃圾
                threshold = settings.spam_threshold_embedding
                if adjusted_confidence < threshold:
                    result["is_spam"] = False
                    logger.info(
                        f"上下文调整后置信度降至 {adjusted_confidence:.2f} "
                        f"(原始 {original_confidence:.2f}, 降低 {confidence_reduction:.2f}), "
                        f"改判为正常消息 [用户:{user_id}]: {', '.join(reasons)}"
                    )
                else:
                    logger.info(
                        f"上下文调整后置信度 {adjusted_confidence:.2f} "
                        f"(原始 {original_confidence:.2f}, 降低 {confidence_reduction:.2f}), "
                        f"仍判定为垃圾 [用户:{user_id}]"
                    )

        except Exception as e:
            logger.error(f"应用上下文调整失败 [用户:{user_id}]: {e}")

        return result

    async def detect_image(
        self,
        image_path: str,
        user_id: int,
        chat_id: int,
        *,
        caption: str | None = None,
        context_text: str | None = None,
        activity: int | None = None,
        skip_auto_train: bool = False,
    ) -> DetectionResult:
        """检测图片是否为垃圾信息（分支入口）

        优先走 AI Vision 直判（省一次 OCR 调用 + 保留视觉信息）。
        Vision 不可用或失败时降级到 OCR → 文本管道。

        Args:
            image_path: 图片文件路径
            user_id: 用户 ID
            chat_id: 群组 ID
            caption: 图片文字说明（可选，Vision 会一起送 AI 判断）
            context_text: 格式化后的群组对话上下文（可选，仅 Vision 使用）
            activity: 用户活跃度（用于 Vision 路径的置信度调整）
            skip_auto_train: 确认模式下跳过自动入库训练样本

        Returns:
            检测结果字典
        """
        # Vision 多模态直判（无 OCR 兜底：不可用/失败则放行不检测）
        empty_result: DetectionResult = {
            "is_spam": False,
            "confidence": 0.0,
            "original_confidence": 0.0,
            "activity_reduction": 0.0,
            "stage": None,
            "reasons": [],
            "details": {},
        }

        if not self.ai_detector.vision_enabled:
            logger.debug(f"Vision 未启用或无可用 provider，跳过图片检测 [用户:{user_id}]")
            return empty_result

        try:
            return await self._detect_image_via_vision(
                image_path=image_path,
                user_id=user_id,
                chat_id=chat_id,
                caption=caption,
                context_text=context_text,
                activity=activity,
                skip_auto_train=skip_auto_train,
            )
        except VisionUnsupportedError as e:
            logger.info(f"Vision 不支持，跳过图片检测 [用户:{user_id}]: {e}")
        except VisionAllFailedError as e:
            logger.warning(f"Vision 全部失败，跳过图片检测 [用户:{user_id}]: {e}")
        except Exception as e:
            logger.warning(f"Vision 异常，跳过图片检测 [用户:{user_id}]: {e}")

        return empty_result

    async def _detect_image_via_vision(
        self,
        *,
        image_path: str,
        user_id: int,
        chat_id: int,
        caption: str | None,
        context_text: str | None,
        activity: int | None,
        skip_auto_train: bool,
    ) -> DetectionResult:
        """AI Vision 直判图片（新路径，省一次 OCR 调用）"""
        # 读图 + 大小检查（线程池执行，避免阻塞事件循环）
        image_b64, mime, image_size = await run_in_executor(_read_image_as_base64, image_path)

        if image_size > settings.ai_spam_vision_max_image_bytes:
            raise VisionUnsupportedError(
                f"图片超过 Vision 大小上限 {settings.ai_spam_vision_max_image_bytes} bytes "
                f"(实际 {image_size} bytes)"
            )

        ai_result = await self.ai_detector.detect_image_with_context(
            image_b64,
            mime,
            caption=caption,
            context_text=context_text,
        )

        extracted_text = str(ai_result.get("details", {}).get("extracted_text", "") or "")
        ai_reason = ai_result["reasons"][0] if ai_result.get("reasons") else "AI Vision 判定"

        # 训练样本入库用的文本兜底：Vision 识别不出文字时，用占位符避免写空串
        sample_text = extracted_text or f"[图片]{caption or ''}".strip() or "[图片]"

        result: DetectionResult = {
            "is_spam": bool(ai_result["is_spam"]),
            "confidence": float(ai_result["confidence"]),
            "original_confidence": float(ai_result["confidence"]),
            "activity_reduction": 0.0,
            "stage": "ai_vision",
            "reasons": ["图片 AI 视觉", ai_reason],
            "details": {
                **ai_result.get("details", {}),
                "recognized_text": sample_text,
                "recognized_text_masked": (
                    mask_text(extracted_text) if extracted_text else "[无文字]"
                ),
                "has_caption": bool(caption),
            },
        }

        # 活跃度调整（与 AI 文本分支保持一致；Vision 已消费 context，跳过上下文一致性调整）
        if result["is_spam"]:
            result = self._apply_activity_adjustment(result, activity, user_id)

        # 样本入库（正样本 / 高置信度负样本），确认模式下跳过避免重复
        if not skip_auto_train:
            if result["is_spam"]:
                await self._handle_ai_spam_detection(sample_text, ai_result, user_id)
            else:
                await self._handle_ai_negative_detection(sample_text, ai_result, user_id)

        if result["is_spam"]:
            logger.info(
                f"检测到图片垃圾信息（Vision）[用户:{user_id}] "
                f"[置信度:{result['confidence']:.2f}] "
                f"[原因:{ai_reason}]"
            )
        else:
            logger.debug(
                f"Vision 判定为正常图片 [用户:{user_id}] " f"[置信度:{result['confidence']:.2f}]"
            )

        return result

    async def add_feedback(
        self, text: str, is_spam: bool, labeled_by: int, confidence: float | None = None
    ) -> bool:
        """添加管理员反馈样本

        Args:
            text: 消息文本
            is_spam: 是否为垃圾
            labeled_by: 标注者 ID
            confidence: 置信度

        Returns:
            是否添加成功
        """
        try:
            await SpamRepository.add_sample(
                text=text,
                is_spam=is_spam,
                confidence=confidence,
                labeled_by=labeled_by,
            )

            logger.info(
                f"已添加反馈样本 [标注者:{labeled_by}] 类型: {'垃圾' if is_spam else '正常'}"
            )
            return True

        except Exception as e:
            logger.error(f"添加反馈样本失败: {e}")
            return False

    async def get_statistics(self) -> dict[str, Any]:
        """获取统计信息"""
        try:
            total_samples = await SpamRepository.count_samples()
            spam_samples = await SpamRepository.count_samples(is_spam=True)
            normal_samples = await SpamRepository.count_samples(is_spam=False)

            return {
                "total_samples": total_samples,
                "spam_samples": spam_samples,
                "normal_samples": normal_samples,
                "classifier_trained": self.classifier.is_trained,
                "embedder_initialized": self.embedder.is_initialized,
            }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {}

    async def retrain_model(self, admin_ids: list[int] | None = None) -> RetrainResult:
        """重新训练模型

        Args:
            admin_ids: 管理员 ID 列表，用于发送训练完成通知

        Returns:
            稳定结果 code 与渲染参数(不含中文文案,由 handler 渲染)
        """
        try:
            # 获取训练数据
            texts, labels = await SpamRepository.get_training_data()

            if len(texts) < _MIN_TRAINING_SAMPLES:
                return RetrainResult(
                    code=RetrainCode.insufficient_samples,
                    params={
                        "current": len(texts),
                        "min_required": _MIN_TRAINING_SAMPLES,
                    },
                )

            # 训练分类器
            # ✅ P1-11: 模型训练是 CPU 密集型操作，在线程池中运行
            accuracy, metrics = await run_in_executor(self.classifier.train, texts, labels)

            # 保存模型
            saved = self.classifier.save_model()

            if not saved:
                return RetrainResult(code=RetrainCode.save_failed, params={})

            # 更新上次训练时的样本数量
            from src.repositories.spam_repo import update_last_train_count

            update_last_train_count(len(texts))

            # 更新上次训练时间（用于冷却时间检查）
            import time

            from src.core.redis import RedisKeys, get_redis

            redis = get_redis()
            await redis.set(RedisKeys.last_train_time(), str(time.time()))

            logger.info(
                f"模型训练成功 [准确率:{accuracy:.2%}] "
                f"[总样本:{metrics['total_samples']}] "
                f"[垃圾样本:{metrics['spam_samples']}] "
                f"[正常样本:{metrics['normal_samples']}]"
            )

            result = RetrainResult(
                code=RetrainCode.success,
                params={
                    "accuracy": float(accuracy),
                    "total_samples": int(metrics["total_samples"]),
                    "spam_samples": int(metrics["spam_samples"]),
                    "normal_samples": int(metrics["normal_samples"]),
                },
            )

            # 如果提供了管理员 ID，发送通知
            if admin_ids:
                await self._notify_admins_training_complete(admin_ids, result)

            return result

        except Exception as e:
            logger.error(f"重新训练模型失败: {e}")
            return RetrainResult(
                code=RetrainCode.failed,
                params={"error": str(e)},
            )

    async def _notify_admins_training_complete(
        self, admin_ids: list[int], result: RetrainResult
    ) -> None:
        """通知管理员训练完成(逐管理员按其 for_user locale 渲染)

        Args:
            admin_ids: 管理员 ID 列表
            result: 训练成功结果(仅 success 时调用)
        """
        try:
            from aiogram import Bot
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode

            from src.core.config import settings
            from src.core.i18n import get_resolver, get_translator

            bot = Bot(
                token=settings.bot_token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            try:
                for admin_id in admin_ids:
                    try:
                        locale = await get_resolver().for_user(admin_id)
                        localizer = get_translator().for_locale(locale)
                        notification = localizer.t(
                            "admin.antispam.auto_train.notification.success.message",
                            accuracy_percent=f"{float(result.params['accuracy']):.2%}",
                            total_samples=result.params["total_samples"],
                            spam_samples=result.params["spam_samples"],
                            normal_samples=result.params["normal_samples"],
                        )
                        await bot.send_message(admin_id, notification)
                        logger.info(f"训练完成通知已发送给管理员 {admin_id}")
                    except Exception as e:
                        logger.warning(f"发送训练通知给管理员 {admin_id} 失败: {e}")
            finally:
                await bot.session.close()
        except Exception as e:
            logger.error(f"发送训练完成通知失败: {e}")

    async def check_and_auto_train(
        self, admin_ids: list[int] | None = None, threshold: int | None = None
    ) -> RetrainResult | None:
        """检查是否需要自动训练，如果需要则触发训练

        Args:
            admin_ids: 管理员 ID 列表，用于发送通知
            threshold: 触发自动训练的新样本阈值（None 则使用配置 AUTO_TRAIN_THRESHOLD）

        Returns:
            已执行训练时返回训练结果(RetrainResult);冷却/未达阈值/检查异常返回 None。
            检查异常属运行故障(非 retrain 业务结果),仅内部日志,不传播。
        """
        try:
            import time

            from src.core.redis import RedisKeys, get_redis
            from src.repositories.spam_repo import get_last_train_count

            # 使用配置的阈值（如果未指定）
            if threshold is None:
                threshold = settings.auto_train_threshold

            # 检查冷却时间
            redis = get_redis()
            last_train_time_str = await redis.get(RedisKeys.last_train_time())

            if last_train_time_str:
                last_train_time = float(last_train_time_str)
                current_time = time.time()
                elapsed_hours = (current_time - last_train_time) / 3600

                if elapsed_hours < settings.auto_train_cooldown_hours:
                    remaining_hours = settings.auto_train_cooldown_hours - elapsed_hours
                    logger.debug(
                        f"训练冷却中: 已过 {elapsed_hours:.1f} 小时，"
                        f"还需 {remaining_hours:.1f} 小时（冷却时间 {settings.auto_train_cooldown_hours} 小时）"
                    )
                    return None

            # 获取当前样本总数
            current_count = await SpamRepository.count_samples()

            # 获取上次训练时的样本数量
            last_count = get_last_train_count()

            # 计算新增样本数
            new_samples = current_count - last_count

            logger.debug(
                f"样本统计: 当前={current_count}, 上次训练={last_count}, 新增={new_samples}"
            )

            # 检查是否达到训练阈值
            if new_samples >= threshold:
                logger.info(f"检测到 {new_samples} 个新样本（阈值={threshold}），触发自动训练...")

                # 触发训练
                result = await self.retrain_model(admin_ids)

                if result.success:
                    # 更新上次训练时间
                    await redis.set(RedisKeys.last_train_time(), str(time.time()))
                return result

            return None

        except Exception as e:
            logger.error(f"检查自动训练失败: {e}")
            return None


# 全局检测器实例
_detector: SpamDetector | None = None


def get_detector() -> SpamDetector:
    """获取全局检测器实例"""
    global _detector
    if _detector is None:
        _detector = SpamDetector()
    return _detector
