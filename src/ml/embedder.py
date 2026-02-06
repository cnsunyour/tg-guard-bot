"""Embedding 语义分析模块 - Stage 3 语义检测"""

import numpy as np
from loguru import logger

try:
    from fastembed import TextEmbedding

    FASTEMBED_AVAILABLE = True
except ImportError:
    FASTEMBED_AVAILABLE = False
    logger.warning("fastembed 未安装，Embedding 功能将不可用")

from src.core.config import settings


class SpamEmbedder:
    """垃圾信息语义嵌入检测器"""

    def __init__(self, model_name: str | None = None):
        """初始化 Embedder

        Args:
            model_name: 模型名称，默认使用配置中的模型
        """
        self.model_name = model_name or settings.embedding_model_name
        self.model: TextEmbedding | None = None
        self.spam_prototypes: list[np.ndarray] = []
        self.is_initialized = False

        # 预定义的垃圾信息原型文本
        self.default_spam_texts = [
            "加微信免费领取礼品，先到先得",
            "点击链接下载APP，注册送现金",
            "兼职刷单日赚500，零投资高回报",
            "美女上门服务，价格优惠",
            "赌博网站充值送钱，提现秒到账",
            "私聊我获取内部消息，稳赚不赔",
            "扫码进群领福利，手慢无",
            "投资理财高收益，月入过万",
        ]

        if not FASTEMBED_AVAILABLE:
            logger.warning("fastembed 不可用，Embedding 功能已禁用")
            return

        # 初始化模型
        self._initialize_model()

    def _initialize_model(self) -> bool:
        """初始化 Embedding 模型"""
        if not FASTEMBED_AVAILABLE:
            return False

        try:
            logger.info(f"正在加载 Embedding 模型: {self.model_name}")

            # 使用 fastembed 加载模型
            # 对于中文，使用 BAAI/bge-small-zh-v1.5
            self.model = TextEmbedding(model_name=self.model_name)

            # 生成默认的垃圾信息原型
            self._generate_prototypes(self.default_spam_texts)

            self.is_initialized = True
            logger.info("Embedding 模型加载成功")
            return True

        except Exception as e:
            logger.error(f"加载 Embedding 模型失败: {e}")
            return False

    def _generate_prototypes(self, texts: list[str]) -> None:
        """生成垃圾信息原型向量

        Args:
            texts: 垃圾信息示例文本列表
        """
        if not self.model:
            return

        try:
            # 批量生成嵌入向量
            embeddings = list(self.model.embed(texts))
            self.spam_prototypes = [np.array(emb) for emb in embeddings]

            logger.info(f"已生成 {len(self.spam_prototypes)} 个垃圾信息原型")

        except Exception as e:
            logger.error(f"生成原型向量失败: {e}")

    def add_spam_prototype(self, text: str) -> bool:
        """添加新的垃圾信息原型

        Args:
            text: 垃圾信息文本

        Returns:
            是否添加成功
        """
        if not self.is_initialized or not self.model:
            return False

        try:
            # 生成嵌入向量
            embedding = next(self.model.embed([text]))  # type: ignore[call-overload]
            self.spam_prototypes.append(np.array(embedding))

            logger.info(f"已添加新的垃圾信息原型，当前数量: {len(self.spam_prototypes)}")
            return True

        except Exception as e:
            logger.error(f"添加原型失败: {e}")
            return False

    def _cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(dot_product / (norm1 * norm2))

    def predict(self, text: str) -> tuple[bool, float]:
        """预测文本是否为垃圾信息

        Args:
            text: 待检测文本

        Returns:
            (是否为垃圾, 最大相似度)
        """
        if not self.is_initialized or not self.model:
            logger.debug("Embedding 模型未初始化")
            return False, 0.0

        if not self.spam_prototypes:
            logger.warning("没有垃圾信息原型")
            return False, 0.0

        try:
            # 生成文本嵌入
            text_embedding = np.array(next(self.model.embed([text])))  # type: ignore[call-overload]

            # 计算与所有原型的相似度
            similarities = [
                self._cosine_similarity(text_embedding, prototype)
                for prototype in self.spam_prototypes
            ]

            # 取最大相似度
            max_similarity = max(similarities)

            # 判断是否为垃圾（相似度超过阈值）
            threshold = settings.spam_threshold_embedding
            is_spam = max_similarity > threshold

            logger.debug(
                f"Embedding 相似度: {max_similarity:.3f}, "
                f"阈值: {threshold}, "
                f"判定: {'垃圾' if is_spam else '正常'}"
            )

            return is_spam, max_similarity

        except Exception as e:
            logger.error(f"Embedding 预测失败: {e}")
            return False, 0.0

    def predict_batch(self, texts: list[str]) -> list[tuple[bool, float]]:
        """批量预测

        Args:
            texts: 文本列表

        Returns:
            预测结果列表 [(是否为垃圾, 相似度), ...]
        """
        if not self.is_initialized or not self.model:
            return [(False, 0.0)] * len(texts)

        if not self.spam_prototypes:
            return [(False, 0.0)] * len(texts)

        try:
            # 批量生成嵌入
            text_embeddings = [np.array(emb) for emb in self.model.embed(texts)]

            results = []
            threshold = settings.spam_threshold_embedding

            for text_embedding in text_embeddings:
                # 计算与所有原型的相似度
                similarities = [
                    self._cosine_similarity(text_embedding, prototype)
                    for prototype in self.spam_prototypes
                ]

                max_similarity = max(similarities)
                is_spam = max_similarity > threshold

                results.append((is_spam, max_similarity))

            return results

        except Exception as e:
            logger.error(f"批量 Embedding 预测失败: {e}")
            return [(False, 0.0)] * len(texts)

    async def embed(self, texts: list[str]) -> list[np.ndarray]:
        """生成文本嵌入向量（异步包装）

        Args:
            texts: 文本列表

        Returns:
            嵌入向量列表
        """
        if not self.is_initialized or not self.model:
            logger.warning("Embedding 模型未初始化")
            return []

        try:
            # fastembed 是同步的，但我们在线程池中运行以避免阻塞
            from src.core.executor import run_in_executor

            def _embed() -> list[np.ndarray]:
                assert self.model is not None  # 类型检查
                return [np.array(emb) for emb in self.model.embed(texts)]

            embeddings = await run_in_executor(_embed)
            return embeddings

        except Exception as e:
            logger.error(f"生成嵌入向量失败: {e}")
            return []

    async def compute_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的余弦相似度

        Args:
            text1: 第一个文本
            text2: 第二个文本

        Returns:
            余弦相似度 (0.0-1.0)
        """
        if not self.is_initialized or not self.model:
            logger.debug("Embedding 模型未初始化")
            return 0.0

        try:
            embeddings = await self.embed([text1, text2])
            if len(embeddings) != 2:
                return 0.0

            similarity = self._cosine_similarity(embeddings[0], embeddings[1])
            return float(similarity)

        except Exception as e:
            logger.error(f"计算相似度失败: {e}")
            return 0.0

    async def detect_context_consistency(
        self,
        text: str,
        context_messages: list[dict],
        high_similarity_threshold: float | None = None,
    ) -> tuple[bool, float]:
        """检测消息是否与上下文语义一致

        ⚠️ 只用于降低误判，不用于提高检测

        Args:
            text: 当前消息文本
            context_messages: 上下文消息列表 [{"text": str, ...}, ...]
            high_similarity_threshold: 高相似度阈值（None 则使用配置）

        Returns:
            (is_consistent, similarity_score)
            - is_consistent: 是否与上下文一致（高相似度）
            - similarity_score: 与上下文的平均相似度
        """
        if not context_messages:
            return False, 0.0

        # 使用配置的阈值
        if high_similarity_threshold is None:
            high_similarity_threshold = settings.context_high_similarity_threshold

        try:
            # 提取上下文文本
            context_texts = [msg["text"] for msg in context_messages if msg.get("text")]
            if not context_texts:
                return False, 0.0

            # 生成嵌入向量
            all_texts = [text, *context_texts]
            embeddings = await self.embed(all_texts)

            if len(embeddings) < 2:
                return False, 0.0

            current_emb = embeddings[0]
            context_embs = embeddings[1:]

            # 计算与上下文的平均相似度
            similarities = [
                self._cosine_similarity(current_emb, ctx_emb) for ctx_emb in context_embs
            ]
            avg_similarity = float(np.mean(similarities))

            # 判断是否一致（高相似度）
            is_consistent = avg_similarity >= high_similarity_threshold

            logger.debug(
                f"上下文一致性检测: 相似度={avg_similarity:.2f}, "
                f"阈值={high_similarity_threshold}, "
                f"一致={is_consistent}"
            )

            return is_consistent, avg_similarity

        except Exception as e:
            logger.error(f"上下文一致性检测失败: {e}")
            return False, 0.0


# 全局 Embedder 实例
_embedder: SpamEmbedder | None = None


def get_embedder() -> SpamEmbedder:
    """获取全局 Embedder 实例"""
    global _embedder
    if _embedder is None:
        _embedder = SpamEmbedder()
    return _embedder
