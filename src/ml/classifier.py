"""TF-IDF + SVM 分类器模块 - Stage 2 机器学习分类"""

import os
import pickle
import hmac
import hashlib
from typing import Optional, List, Tuple

import jieba
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.svm import LinearSVC
from sklearn.pipeline import Pipeline
from loguru import logger

from src.core.config import settings


class SpamClassifier:
    """垃圾信息分类器 - TF-IDF + SVM"""

    def __init__(self, model_path: Optional[str] = None):
        """初始化分类器

        Args:
            model_path: 模型文件路径，如果为 None 则使用配置中的路径
        """
        self.model_path = model_path or settings.ml_model_path
        self.model: Optional[Pipeline] = None
        self.is_trained = False

        # 尝试加载已有模型
        self.load_model()

    def _tokenize(self, text: str) -> List[str]:
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

    def train(
        self, texts: List[str], labels: List[bool]
    ) -> Tuple[float, dict]:
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

    def predict(self, text: str) -> Tuple[bool, float]:
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
                f"预测结果: {'垃圾' if prediction else '正常'}, "
                f"置信度: {confidence:.2f}"
            )

            return bool(prediction), float(confidence)

        except Exception as e:
            logger.error(f"预测失败: {e}")
            return False, 0.0

    def predict_batch(self, texts: List[str]) -> List[Tuple[bool, float]]:
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
                for pred, conf in zip(predictions, confidences)
            ]

            return results

        except Exception as e:
            logger.error(f"批量预测失败: {e}")
            return [(False, 0.0)] * len(texts)

    def save_model(self, path: Optional[str] = None) -> bool:
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

            # 序列化模型
            model_data = pickle.dumps(self.model)

            # 生成签名
            signature = hmac.new(
                settings.model_signature_key.encode(),
                model_data,
                hashlib.sha256
            ).hexdigest()

            # 保存：签名 + 换行 + 数据
            with open(save_path, "wb") as f:
                f.write(signature.encode() + b'\n' + model_data)

            logger.info(f"模型已保存到: {save_path} (已签名)")
            return True

        except Exception as e:
            logger.error(f"保存模型失败: {e}")
            return False

    def load_model(self, path: Optional[str] = None) -> bool:
        """加载模型（验证签名）

        Args:
            path: 模型路径，如果为 None 则使用 self.model_path
        """
        load_path = path or self.model_path

        if not os.path.exists(load_path):
            logger.info(f"模型文件不存在: {load_path}")
            return False

        try:
            with open(load_path, "rb") as f:
                content = f.read()

            # 分离签名和数据
            parts = content.split(b'\n', 1)

            if len(parts) != 2:
                # 尝试作为旧格式（无签名）加载
                logger.warning(f"模型文件无签名，可能是旧版本: {load_path}")
                try:
                    self.model = pickle.loads(content)
                    self.is_trained = True
                    logger.warning("已加载无签名模型，建议重新训练并保存")
                    return True
                except Exception as e:
                    logger.error(f"加载旧格式模型失败: {e}")
                    return False

            saved_signature = parts[0].decode()
            model_data = parts[1]

            # 验证签名
            expected_signature = hmac.new(
                settings.model_signature_key.encode(),
                model_data,
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(saved_signature, expected_signature):
                logger.error(f"模型签名验证失败！文件可能被篡改: {load_path}")
                return False

            # 反序列化模型
            self.model = pickle.loads(model_data)
            self.is_trained = True
            logger.info(f"模型已加载: {load_path} (签名验证通过)")
            return True

        except Exception as e:
            logger.error(f"加载模型失败: {e}")
            return False


# 全局分类器实例
_classifier: Optional[SpamClassifier] = None


def get_classifier() -> SpamClassifier:
    """获取全局分类器实例"""
    global _classifier
    if _classifier is None:
        _classifier = SpamClassifier()
    return _classifier
