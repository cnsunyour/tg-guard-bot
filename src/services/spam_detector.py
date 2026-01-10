"""反垃圾检测服务 - 整合三阶段管道

✅ P1-11: CPU 密集型操作已移至线程池
"""

from typing import Any

from loguru import logger

from src.core.config import settings
from src.core.executor import run_in_executor  # ✅ P1-11: 导入线程池执行器
from src.core.utils import mask_text
from src.ml.classifier import get_classifier
from src.ml.embedder import get_embedder
from src.ml.ocr import get_ocr_extractor
from src.ml.rule_engine import get_rule_engine
from src.repositories.spam_repo import SpamRepository


class SpamDetector:
    """反垃圾检测服务 - 三阶段管道"""

    def __init__(self):
        """初始化检测器"""
        self.rule_engine = get_rule_engine()
        self.classifier = get_classifier()
        self.embedder = get_embedder()
        self.ocr_extractor = get_ocr_extractor()

    async def detect(
        self, text: str, user_id: int, chat_id: int, activity: int | None = None
    ) -> dict[str, Any]:
        """检测文本是否为垃圾信息

        Args:
            text: 待检测文本
            user_id: 用户 ID
            chat_id: 群组 ID
            activity: 用户活跃度（可选），用于置信度调整

        Returns:
            检测结果字典
        """
        result = {
            "is_spam": False,
            "confidence": 0.0,
            "original_confidence": 0.0,  # 原始置信度
            "activity_reduction": 0.0,  # 活跃度减少值
            "stage": None,
            "reasons": [],
            "details": {},
        }

        # Stage 1: 规则引擎（快速过滤）
        # ✅ P1-11: 在线程池中运行，避免阻塞事件循环
        rule_result = await run_in_executor(self.rule_engine.analyze, text)

        if rule_result["is_spam"]:
            result["is_spam"] = True
            result["confidence"] = rule_result["confidence"]
            result["stage"] = "rule_engine"
            result["reasons"] = rule_result["reasons"]
            result["details"] = rule_result["details"]

            logger.info(
                f"Stage 1 检测到垃圾信息 [用户:{user_id}] "
                f"原因: {', '.join(rule_result['reasons'])}"
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
                        f"Stage 2 检测到垃圾信息 [用户:{user_id}] " f"置信度: {confidence_ml:.2f}"
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

                    logger.info(
                        f"Stage 3 检测到垃圾信息 [用户:{user_id}] " f"相似度: {similarity:.2f}"
                    )

                    # 应用活跃度置信度调整
                    result = self._apply_activity_adjustment(result, activity, user_id)
                    return result

            except Exception as e:
                logger.error(f"Embedding 检测失败: {e}")

        # 未检测到垃圾信息
        logger.debug(f"消息通过检测 [用户:{user_id}]")
        return result

    def _apply_activity_adjustment(
        self, result: dict[str, Any], activity: int | None, user_id: int
    ) -> dict[str, Any]:
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

    async def detect_image(self, image_path: str, user_id: int, chat_id: int) -> dict[str, Any]:
        """检测图片是否为垃圾信息（通过 OCR 提取文字）

        Args:
            image_path: 图片文件路径
            user_id: 用户 ID
            chat_id: 群组 ID

        Returns:
            检测结果字典
        """
        result = {
            "is_spam": False,
            "confidence": 0.0,
            "stage": None,
            "reasons": [],
            "details": {},
        }

        # 检查 OCR 是否可用
        if not self.ocr_extractor.is_available:
            logger.warning("OCR 不可用，跳过图片检测")
            return result

        # 提取图片中的文字
        try:
            # ✅ P1-11: OCR 是 CPU 密集型操作，在线程池中运行
            extracted_text = await run_in_executor(self.ocr_extractor.extract_text, image_path)

            if not extracted_text:
                logger.debug(f"图片中未提取到文字 [用户:{user_id}]")
                return result

            logger.info(f"从图片提取文字 [用户:{user_id}] " f"内容: {mask_text(extracted_text)}")

            # 使用文本检测管道检测提取的文字
            text_result = await self.detect(text=extracted_text, user_id=user_id, chat_id=chat_id)

            if text_result["is_spam"]:
                # 标记为图片垃圾
                text_result["reasons"].insert(0, "图片 OCR")
                # ✅ 保存完整文本用于训练，脱敏文本用于日志
                text_result["details"]["ocr_text"] = extracted_text  # 仅用于训练，不记录到日志
                text_result["details"]["ocr_text_masked"] = mask_text(extracted_text)  # 用于日志

                logger.info(
                    f"检测到图片垃圾信息 [用户:{user_id}] "
                    f"阶段: {text_result['stage']}, "
                    f"原因: {', '.join(text_result['reasons'])}"
                )

            return text_result

        except Exception as e:
            logger.error(f"图片检测失败 [用户:{user_id}]: {e}")
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
                f"已添加反馈样本 [标注者:{labeled_by}] " f"类型: {'垃圾' if is_spam else '正常'}"
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

    async def retrain_model(self, admin_ids: list[int] | None = None) -> tuple[bool, str]:
        """重新训练模型

        Args:
            admin_ids: 管理员 ID 列表，用于发送训练完成通知

        Returns:
            (是否成功, 消息)
        """
        try:
            # 获取训练数据
            texts, labels = await SpamRepository.get_training_data()

            if len(texts) < 10:
                return False, f"训练样本不足: {len(texts)}，至少需要 10 个样本"

            # 训练分类器
            # ✅ P1-11: 模型训练是 CPU 密集型操作，在线程池中运行
            accuracy, metrics = await run_in_executor(self.classifier.train, texts, labels)

            # 保存模型
            saved = self.classifier.save_model()

            if not saved:
                return False, "模型训练成功但保存失败"

            # 更新上次训练时的样本数量
            from src.repositories.spam_repo import update_last_train_count

            update_last_train_count(len(texts))

            message = (
                f"模型训练成功！\n"
                f"准确率: {accuracy:.2%}\n"
                f"总样本: {metrics['total_samples']}\n"
                f"垃圾样本: {metrics['spam_samples']}\n"
                f"正常样本: {metrics['normal_samples']}"
            )

            logger.info(message)

            # 如果提供了管理员 ID，发送通知
            if admin_ids:
                await self._notify_admins_training_complete(admin_ids, message)

            return True, message

        except Exception as e:
            logger.error(f"重新训练模型失败: {e}")
            return False, f"训练失败: {e!s}"

    async def _notify_admins_training_complete(self, admin_ids: list[int], message: str) -> None:
        """通知管理员训练完成

        Args:
            admin_ids: 管理员 ID 列表
            message: 训练结果消息
        """
        try:
            from aiogram import Bot

            from src.core.config import settings

            bot = Bot(token=settings.bot_token)

            notification = f"🤖 <b>反垃圾模型自动训练完成</b>\n\n{message}"

            for admin_id in admin_ids:
                try:
                    await bot.send_message(admin_id, notification)
                    logger.info(f"训练完成通知已发送给管理员 {admin_id}")
                except Exception as e:
                    logger.warning(f"发送训练通知给管理员 {admin_id} 失败: {e}")

            await bot.session.close()

        except Exception as e:
            logger.error(f"发送训练完成通知失败: {e}")

    async def check_and_auto_train(
        self, admin_ids: list[int] | None = None, threshold: int = 50
    ) -> tuple[bool, str | None]:
        """检查是否需要自动训练，如果需要则触发训练

        Args:
            admin_ids: 管理员 ID 列表，用于发送通知
            threshold: 触发自动训练的新样本阈值（默认 50）

        Returns:
            (是否触发了训练, 消息)
        """
        try:
            from src.repositories.spam_repo import get_last_train_count

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
                success, message = await self.retrain_model(admin_ids)

                if success:
                    return True, f"自动训练成功: {message}"
                else:
                    return False, f"自动训练失败: {message}"

            return False, None

        except Exception as e:
            logger.error(f"检查自动训练失败: {e}")
            return False, f"检查失败: {e!s}"


# 全局检测器实例
_detector: SpamDetector | None = None


def get_detector() -> SpamDetector:
    """获取全局检测器实例"""
    global _detector
    if _detector is None:
        _detector = SpamDetector()
    return _detector
