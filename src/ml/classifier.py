"""TF-IDF + SVM 分类器模块 - Stage 2 机器学习分类"""

import hashlib
import hmac
import io
import os
import stat

import jieba
import joblib
import numpy as np
from loguru import logger
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

from src.core.config import settings


class SpamClassifier:
    """垃圾信息分类器 - TF-IDF + SVM"""

    def __init__(self, model_path: str | None = None):
        """初始化分类器

        Args:
            model_path: 模型文件路径，如果为 None 则使用配置中的路径
        """
        self.model_path = model_path or settings.ml_model_path
        self.model: Pipeline | None = None
        self.is_trained = False

        # 尝试加载已有模型
        self.load_model()

    def _tokenize(self, text: str) -> list[str]:
        """中文分词

        Args:
            text: 待分词文本

        Returns:
            分词结果列表
        """
        # 使用 jieba 进行中文分词
        words = jieba.cut(text)
        # 过滤停用词和单字符
        return [w for w in words if len(w) > 1]

    def _create_pipeline(self) -> Pipeline:
        """创建分类管道"""
        return Pipeline(
            [
                (
                    "tfidf",
                    TfidfVectorizer(
                        tokenizer=self._tokenize,
                        ngram_range=(1, 2),  # 单字和双字组合
                        max_features=5000,  # 最多保留5000个特征
                        min_df=2,  # 至少在2个文档中出现
                        max_df=0.8,  # 最多在80%文档中出现
                    ),
                ),
                (
                    "svm",
                    LinearSVC(
                        C=1.0,
                        class_weight="balanced",  # 平衡类别权重
                        max_iter=1000,
                        random_state=42,
                    ),
                ),
            ]
        )

    def train(self, texts: list[str], labels: list[bool]) -> tuple[float, dict]:
        """训练模型

        Args:
            texts: 文本列表
            labels: 标签列表 (True=垃圾, False=正常)

        Returns:
            (准确率, 详细指标)
        """
        if len(texts) != len(labels):
            raise ValueError("文本和标签数量不匹配")

        if len(texts) < 10:
            raise ValueError(f"训练样本太少: {len(texts)}，至少需要10个样本")

        logger.info(f"开始训练模型，样本数: {len(texts)}")

        # 创建新的管道
        self.model = self._create_pipeline()

        # 训练模型
        self.model.fit(texts, labels)
        self.is_trained = True

        # 计算训练集准确率（简单评估）
        predictions = self.model.predict(texts)
        accuracy = np.mean(predictions == np.array(labels))

        # 统计各类别样本数
        spam_count = sum(labels)
        normal_count = len(labels) - spam_count

        metrics = {
            "accuracy": accuracy,
            "total_samples": len(texts),
            "spam_samples": spam_count,
            "normal_samples": normal_count,
        }

        logger.info(
            f"模型训练完成，准确率: {accuracy:.2%}, "
            f"垃圾样本: {spam_count}, 正常样本: {normal_count}"
        )

        return accuracy, metrics

    def predict(self, text: str) -> tuple[bool, float]:
        """预测文本是否为垃圾信息

        Args:
            text: 待检测文本

        Returns:
            (是否为垃圾, 置信度)
        """
        if not self.is_trained or self.model is None:
            logger.warning("模型未训练，无法进行预测")
            return False, 0.0

        try:
            # 预测
            prediction = self.model.predict([text])[0]

            # 获取决策函数值作为置信度
            decision = self.model.decision_function([text])[0]

            # 将决策函数值转换为 0-1 之间的置信度
            # decision 值越大，越确定是垃圾信息
            confidence = 1.0 / (1.0 + np.exp(-decision))  # sigmoid

            logger.debug(
                f"预测结果: {'垃圾' if prediction else '正常'}, " f"置信度: {confidence:.2f}"
            )

            return bool(prediction), float(confidence)

        except Exception as e:
            logger.error(f"预测失败: {e}")
            return False, 0.0

    def predict_batch(self, texts: list[str]) -> list[tuple[bool, float]]:
        """批量预测

        Args:
            texts: 文本列表

        Returns:
            预测结果列表 [(是否为垃圾, 置信度), ...]
        """
        if not self.is_trained or self.model is None:
            logger.warning("模型未训练，无法进行预测")
            return [(False, 0.0)] * len(texts)

        try:
            # 批量预测
            predictions = self.model.predict(texts)
            decisions = self.model.decision_function(texts)

            # 转换为置信度
            confidences = 1.0 / (1.0 + np.exp(-decisions))

            results = [
                (bool(pred), float(conf))
                for pred, conf in zip(predictions, confidences, strict=False)
            ]

            return results

        except Exception as e:
            logger.error(f"批量预测失败: {e}")
            return [(False, 0.0)] * len(texts)

    def save_model(self, path: str | None = None) -> bool:
        """保存模型（带数字签名）

        Args:
            path: 保存路径，如果为 None 则使用 self.model_path
        """
        if not self.is_trained or self.model is None:
            logger.warning("模型未训练，无法保存")
            return False

        save_path = path or self.model_path

        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            # 序列化模型到字节流
            buffer = io.BytesIO()
            joblib.dump(self.model, buffer, compress=3)
            model_data = buffer.getvalue()

            # 生成签名
            signature = hmac.new(
                settings.model_signature_key.encode(), model_data, hashlib.sha256
            ).hexdigest()

            # 保存：签名 + 换行 + 数据
            with open(save_path, "wb") as f:
                f.write(signature.encode() + b"\n" + model_data)

            logger.info(f"模型已保存到: {save_path} (已签名)")
            return True

        except Exception as e:
            logger.error(f"保存模型失败: {e}")
            return False

    def load_model(self, path: str | None = None) -> bool:
        """加载模型（验证签名）

        Args:
            path: 模型路径，如果为 None 则使用 self.model_path
        """
        load_path = path or self.model_path

        if not os.path.exists(load_path):
            logger.info(f"模型文件不存在: {load_path}")
            return False

        # ✅ 安全加固：额外的模型文件安全检查
        try:
            # 1. 检查文件大小（防止加载过大文件）
            MAX_MODEL_SIZE = 100 * 1024 * 1024  # 100MB 上限
            # 拒绝符号链接，避免路径劫持/TOCTOU
            if os.path.islink(load_path):
                logger.error(f"模型文件是符号链接，拒绝加载: {load_path}")
                return False

            file_stat = os.stat(load_path)
            file_mode = file_stat.st_mode

            if not stat.S_ISREG(file_mode):
                logger.error(f"模型路径不是普通文件，拒绝加载: {load_path}")
                return False

            file_size = file_stat.st_size
            if file_size > MAX_MODEL_SIZE:
                logger.error(
                    f"🔒 模型文件过大: {file_size} bytes (限制: {MAX_MODEL_SIZE} bytes)\n"
                    f"文件: {load_path}\n"
                    f"这可能是恶意文件，拒绝加载"
                )
                return False

            # 2. 检查文件权限（只有 owner 应该能写）
            # 检查是否 group 或 others 可写
            if file_mode & (stat.S_IWGRP | stat.S_IWOTH):
                logger.warning(
                    f"⚠️  模型文件权限不安全：group/others 可写\n"
                    f"文件: {load_path}\n"
                    f"建议：chmod 600 {load_path}"
                )

            # 3. 生产环境强制禁用 allow_unsigned_models
            if not settings.debug and settings.allow_unsigned_models:
                logger.error(
                    f"🔒 生产环境禁止启用 ALLOW_UNSIGNED_MODELS\n"
                    f"这会允许加载未签名模型，存在 RCE 风险\n"
                    f"请设置 ALLOW_UNSIGNED_MODELS=false"
                )
                return False

        except Exception as e:
            logger.error(f"检查模型文件安全性失败: {e}")
            return False

        try:
            with open(load_path, "rb") as f:
                # 兜底：即使存在 TOCTOU，也避免读取超大文件导致内存 DoS
                content = f.read(MAX_MODEL_SIZE + 1)

            if len(content) > MAX_MODEL_SIZE:
                logger.error(
                    f"模型文件读取超出限制: {len(content)} bytes (限制: {MAX_MODEL_SIZE} bytes)\n"
                    f"文件: {load_path}"
                )
                return False

            # 分离签名和数据
            parts = content.split(b"\n", 1)

            if len(parts) != 2:
                # 检查是否允许加载未签名模型
                if not settings.allow_unsigned_models:
                    logger.error(
                        f"🔒 安全警告：模型文件无签名，拒绝加载！\n"
                        f"文件: {load_path}\n"
                        f"如需加载旧版本模型，请设置环境变量 ALLOW_UNSIGNED_MODELS=true\n"
                        f"⚠️  强烈建议：重新训练模型并使用签名保存（防止代码执行攻击）"
                    )
                    return False

                # 尝试作为旧格式（无签名）加载
                logger.warning(
                    f"⚠️  安全警告：正在加载无签名模型！\n"
                    f"文件: {load_path}\n"
                    f"⚠️  这可能存在代码执行（RCE）风险，强烈建议重新训练模型"
                )
                try:
                    buffer = io.BytesIO(content)
                    self.model = joblib.load(buffer)
                    self.is_trained = True
                    logger.warning("已加载无签名模型，强烈建议立即重新训练并保存")
                    return True
                except Exception as e:
                    logger.error(f"加载旧格式模型失败: {e}")
                    return False

            saved_signature = parts[0].decode()
            model_data = parts[1]

            # 验证签名
            expected_signature = hmac.new(
                settings.model_signature_key.encode(), model_data, hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(saved_signature, expected_signature):
                logger.error(f"模型签名验证失败！文件可能被篡改: {load_path}")
                return False

            # 反序列化模型
            buffer = io.BytesIO(model_data)
            self.model = joblib.load(buffer)
            self.is_trained = True
            logger.info(f"模型已加载: {load_path} (签名验证通过)")
            return True

        except Exception as e:
            logger.error(f"加载模型失败: {e}")
            return False


# 全局分类器实例
_classifier: SpamClassifier | None = None


def get_classifier() -> SpamClassifier:
    """获取全局分类器实例"""
    global _classifier
    if _classifier is None:
        _classifier = SpamClassifier()
    return _classifier
