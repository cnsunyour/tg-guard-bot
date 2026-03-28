"""AI API 垃圾检测模块 - 支持主备双服务商自动切换

架构设计：
- AIServiceProvider: 抽象基类
- PrimaryAIServiceProvider: 主服务商
- BackupAIServiceProvider: 备份服务商
- HybridAIDetector: 协调器（熔断器、统计追踪、自动回退）
- AISpamDetector: 向后兼容包装

参考实现：src/ml/hybrid_ocr.py
"""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from loguru import logger

from src.core.config import settings

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class AIServiceConfig:
    """AI 服务商配置"""

    enabled: bool = False
    api_key: str = ""
    api_base: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.0
    threshold: float = 0.8
    timeout: int = 10
    max_retries: int = 2
    max_length: int = 500
    client_idle_rebuild_minutes: int = 60
    client_max_lifetime_hours: int = 24


@dataclass
class AIServiceStats:
    """AI 服务商统计信息"""

    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_failure_time: datetime | None = None
    last_error: str = ""

    def record_success(self) -> None:
        """记录成功"""
        self.success_count += 1
        self.consecutive_failures = 0

    def record_failure(self, error: str) -> None:
        """记录失败"""
        self.failure_count += 1
        self.consecutive_failures += 1
        self.last_failure_time = datetime.now()
        self.last_error = error

    def reset_circuit(self) -> None:
        """重置熔断器"""
        self.consecutive_failures = 0
        self.last_failure_time = None


@dataclass
class AIDetectionResult:
    """AI 检测结果（带服务商信息）"""

    is_spam: bool
    confidence: float
    stage: str = "ai_api"
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    provider: str = "unknown"
    attempt_count: int = 0


# ============================================================================
# 异常类
# ============================================================================


class AIServiceError(Exception):
    """AI 服务商基础异常"""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"[{provider}] {message}")


# ============================================================================
# System Prompts
# ============================================================================


# System Prompt - 垃圾检测专用
SYSTEM_PROMPT = """你是垃圾信息检测助手。判断用户消息是否为垃圾信息（广告、赌博、色情、诈骗、推广、引流等）。

严格按照以下 JSON 格式返回结果：
{
  "is_spam": true 或 false,
  "confidence": 0.0-1.0 之间的数字（保留两位小数，如 0.85）,
  "reason": "简短说明判断理由（一句话）"
}

**重要：confidence 的语义定义**
confidence 始终表示"是垃圾"的置信度/概率（保留两位小数）：
- 如果判定为垃圾（is_spam=true），confidence 应该较高（如 0.90 = 90% 确信是垃圾）
- 如果判定为正常（is_spam=false），confidence 应该较低（如 0.10 = 10% 可能是垃圾）

示例：
1. 明显的垃圾广告 → {"is_spam": true, "confidence": 0.95, "reason": "包含明显广告推广"}
2. 正常技术讨论 → {"is_spam": false, "confidence": 0.05, "reason": "正常技术交流内容"}
3. 稍可疑的消息 → {"is_spam": false, "confidence": 0.30, "reason": "内容略有推广倾向但可能是正常分享"}

判断标准：
- 广告推广：加微信、领红包、点击链接、扫码关注等
- 赌博相关：博彩、下注、赔率、稳赚等
- 色情内容：约炮、上门、裸聊、成人服务等
- 诈骗欺诈：刷单、兼职、贷款、投资理财等
- 引流推广：加群、关注公众号、下载 APP 等
- 正常消息：日常聊天、技术讨论、咨询问题等

重要：只返回 JSON，不要返回其他任何内容。"""

# System Prompt - 带上下文的垃圾检测
SYSTEM_PROMPT_WITH_CONTEXT = """你是垃圾信息检测助手。判断用户消息是否为垃圾信息（广告、赌博、色情、诈骗、推广、引流等）。

严格按照以下 JSON 格式返回结果：
{
  "is_spam": true 或 false,
  "confidence": 0.0-1.0 之间的数字（保留两位小数，如 0.85）,
  "reason": "简短说明判断理由（一句话）"
}

**重要：请结合对话上下文进行判断**
- 如果提供了【对话回复链】，优先参考回复链判断消息是否为正常对话的一部分
- 如果提供了【群组最近对话】，参考群组讨论主题判断消息是否相关
- 示例：群里讨论"哪里买手机壳" → 发淘宝链接 → 判断为正常回答而非垃圾

**重要：confidence 的语义定义**
confidence 始终表示"是垃圾"的置信度/概率（保留两位小数）：
- 如果判定为垃圾（is_spam=true），confidence 应该较高（如 0.90 = 90% 确信是垃圾）
- 如果判定为正常（is_spam=false），confidence 应该较低（如 0.10 = 10% 可能是垃圾）

判断标准：
- 广告推广：加微信、领红包、点击链接、扫码关注等
- 赌博相关：博彩、下注、赔率、稳赚等
- 色情内容：约炮、上门、裸聊、成人服务等
- 诈骗欺诈：刷单、兼职、贷款、投资理财等
- 引流推广：加群、关注公众号、下载 APP 等
- 正常消息：日常聊天、技术讨论、咨询问题、回答他人问题等

重要：只返回 JSON，不要返回其他任何内容。"""


# ============================================================================
# AI 服务商抽象基类
# ============================================================================


class AIServiceProvider(ABC):
    """AI 服务商抽象基类

    所有 AI 服务商实现必须实现此接口
    """

    def __init__(self, name: str, config: AIServiceConfig):
        """初始化服务商

        Args:
            name: 服务商名称（用于日志和统计）
            config: 服务商配置
        """
        self.name = name
        self.config = config
        self.client: httpx.AsyncClient | None = None
        self._client_rebuild_pending = False
        self._client_rebuild_reason = ""
        self._client_created_at: datetime | None = None
        self._client_last_used_at: datetime | None = None
        self._client_rebuild_count = 0
        self._last_client_rebuild_at: datetime | None = None
        self._last_client_rebuild_reason = ""
        self._client_lock = asyncio.Lock()

    def _create_client(self) -> httpx.AsyncClient:
        """创建新的 HTTP 客户端

        Returns:
            HTTP 客户端实例
        """
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    def request_client_rebuild(self, reason: str) -> None:
        """标记需要重建 HTTP 客户端

        Args:
            reason: 重建原因
        """
        # 如果已经标记为熔断触发，不允许用其他原因覆盖（熔断优先级最高）
        if self._client_rebuild_pending and self._client_rebuild_reason == "circuit_breaker_tripped":
            return

        # 如果已经有标记且新原因不是熔断，则不更新（保持第一个原因）
        if self._client_rebuild_pending and reason != "circuit_breaker_tripped":
            return

        self._client_rebuild_pending = True
        self._client_rebuild_reason = reason
        logger.debug(f"{self.name} HTTP 客户端已标记重建 [reason={reason}]")

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """检查服务商是否可用

        Returns:
            True 如果服务商已正确配置且可用
        """
        pass

    @abstractmethod
    async def detect(self, text: str, use_context_prompt: bool = False) -> AIDetectionResult:
        """检测文本是否为垃圾信息

        Args:
            text: 待检测文本
            use_context_prompt: 是否使用上下文 Prompt

        Returns:
            AIDetectionResult 检测结果

        Raises:
            AIServiceError: 检测过程中发生错误
        """
        pass

    async def _ensure_client(self) -> httpx.AsyncClient:
        """确保 HTTP 客户端已创建（延迟初始化）

        这是唯一的重建消费点，负责：
        1. 如果没有待重建标记且 client 已存在，检查是否需要因生命周期超时而重建
        2. 如果没有待重建标记且无需重建，直接复用
        3. 如果存在待重建标记，关闭旧 client 并创建新的

        Returns:
            HTTP 客户端实例
        """
        async with self._client_lock:
            now = datetime.now()

            # 没有待重建标记且 client 已存在，先检查生命周期是否超时
            if self.client is not None and not self._client_rebuild_pending:
                client_age = (
                    now - self._client_created_at if self._client_created_at is not None else None
                )
                client_idle = (
                    now - self._client_last_used_at if self._client_last_used_at is not None else None
                )

                if client_age is not None and client_age >= timedelta(
                    hours=self.config.client_max_lifetime_hours
                ):
                    self.request_client_rebuild("max_lifetime_exceeded")
                elif client_idle is not None and client_idle >= timedelta(
                    minutes=self.config.client_idle_rebuild_minutes
                ):
                    self.request_client_rebuild("idle_timeout")
                else:
                    return self.client

            # 需要重建或首次创建
            old_client = None
            rebuild_reason = self._client_rebuild_reason

            if self._client_rebuild_pending:
                old_client = self.client
                self.client = None
                self._client_rebuild_pending = False
                self._client_rebuild_reason = ""
                self._client_created_at = None
                self._client_last_used_at = None

            # 关闭旧 client
            if old_client is not None:
                try:
                    await old_client.aclose()
                except Exception as e:
                    logger.debug(f"关闭 {self.name} 待重建 HTTP 客户端时出现错误（已忽略）: {e}")

            # 创建新 client
            if self.client is None:
                self.client = self._create_client()
                self._client_created_at = now
                self._client_last_used_at = now
                if rebuild_reason:
                    self._client_rebuild_count += 1
                    self._last_client_rebuild_at = now
                    self._last_client_rebuild_reason = rebuild_reason
                    logger.debug(f"{self.name} HTTP 客户端已重建 [reason={rebuild_reason}]")

            return self.client

    async def _call_api(self, text: str, use_context_prompt: bool = False) -> dict[str, Any]:
        """调用 OpenAI 兼容 API

        Args:
            text: 待检测文本
            use_context_prompt: 是否使用上下文 Prompt

        Returns:
            API 响应 JSON

        Raises:
            AIServiceError: HTTP 请求错误或响应解析错误
        """
        # 选择 Prompt
        system_prompt = SYSTEM_PROMPT_WITH_CONTEXT if use_context_prompt else SYSTEM_PROMPT

        # 构建请求
        url = f"{self.config.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        }

        # 发送请求
        client = await self._ensure_client()
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        finally:
            self._client_last_used_at = datetime.now()

        # 解析响应
        data = response.json()
        if "choices" not in data or len(data["choices"]) == 0:
            raise ValueError("API 响应格式错误：缺少 choices")

        content = data["choices"][0].get("message", {}).get("content", "")
        if not content:
            raise ValueError("API 响应内容为空")

        # 解析 JSON 响应
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # 尝试非贪婪正则提取 JSON
            json_match = re.search(r"\{.*?\}", content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise ValueError(f"无法解析 AI 响应为 JSON: {content[:100]}")

        # 验证必需字段
        if not isinstance(result, dict):
            raise ValueError(f"AI 响应不是字典: {type(result)}")
        if "is_spam" not in result or "confidence" not in result:
            raise ValueError(f"AI 响应缺少必需字段: {result}")

        return result

    def _process_result(self, result: dict[str, Any]) -> AIDetectionResult:
        """处理 API 响应，转换为统一格式

        Args:
            result: API 返回的 JSON 结果

        Returns:
            AIDetectionResult 检测结果
        """
        import math

        # 严格验证 is_spam（防止字符串 "false" 被当作 True）
        raw_is_spam = result.get("is_spam", False)
        if isinstance(raw_is_spam, bool):
            is_spam = raw_is_spam
        elif isinstance(raw_is_spam, str):
            is_spam = raw_is_spam.lower() in ("true", "1", "yes")
        else:
            is_spam = bool(raw_is_spam)

        # 严格验证并限制 confidence（防止 NaN/Inf/>1）
        raw_confidence = result.get("confidence", 0.0)
        try:
            confidence = float(raw_confidence)
            # 检查是否为有限数字
            if not math.isfinite(confidence):
                logger.warning(f"AI 返回非法 confidence: {raw_confidence}，使用默认值 0.0")
                confidence = 0.0
            # 限制在 [0.0, 1.0]
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError) as e:
            logger.warning(f"AI 返回无效 confidence: {raw_confidence}，错误: {e}，使用默认值 0.0")
            confidence = 0.0

        # 强制转换 reason 为字符串
        raw_reason = result.get("reason", "无理由")
        reason = str(raw_reason) if raw_reason else "无理由"

        # 根据阈值判断
        final_is_spam = is_spam and confidence >= self.config.threshold

        # 构建返回结果
        return AIDetectionResult(
            is_spam=final_is_spam,
            confidence=confidence,
            stage="ai_api",
            reasons=[reason] if reason else [],
            details={
                "raw_is_spam": is_spam,
                "raw_confidence": confidence,
                "threshold": self.config.threshold,
                "model": self.config.model,
            },
            provider=self.name,
        )

    async def close(self):
        """关闭 HTTP 客户端"""
        async with self._client_lock:
            client = self.client
            self.client = None
            self._client_created_at = None
            self._client_last_used_at = None
            # 注意：不清除 _client_rebuild_pending，因为 close() 可能是切换/熔断流程的一部分
            # 真正的清除会在 _ensure_client() 创建新 client 时进行

            if client is not None:
                try:
                    await client.aclose()
                    logger.debug(f"{self.name} HTTP 客户端已关闭")
                except Exception as e:
                    logger.debug(f"关闭 {self.name} HTTP 客户端时出现错误（已忽略）: {e}")


# ============================================================================
# 主服务商实现
# ============================================================================


class PrimaryAIServiceProvider(AIServiceProvider):
    """主 AI 服务商"""

    def __init__(self):
        config = AIServiceConfig(
            enabled=settings.ai_spam_enabled,
            api_key=settings.ai_spam_api_key,
            api_base=settings.ai_spam_api_base,
            model=settings.ai_spam_model,
            temperature=settings.ai_spam_temperature,
            threshold=settings.ai_spam_threshold,
            timeout=settings.ai_spam_timeout,
            max_retries=settings.ai_spam_max_retries,
            max_length=settings.ai_spam_max_length,
            client_idle_rebuild_minutes=settings.ai_spam_client_idle_rebuild_minutes,
            client_max_lifetime_hours=settings.ai_spam_client_max_lifetime_hours,
        )
        super().__init__("primary", config)

    @property
    def is_available(self) -> bool:
        """检查主服务商是否可用"""
        return self.config.enabled and bool(self.config.api_key)

    async def detect(self, text: str, use_context_prompt: bool = False) -> AIDetectionResult:
        """检测文本是否为垃圾信息（带重试）"""
        # 文本长度限制
        if len(text) > self.config.max_length:
            logger.warning(
                f"文本过长，截断处理 [length={len(text)}] [max={self.config.max_length}]"
            )
            text = text[: self.config.max_length]

        # 带重试的 API 调用
        for attempt in range(self.config.max_retries + 1):
            try:
                result = await self._call_api(text, use_context_prompt)
                detection_result = self._process_result(result)
                detection_result.attempt_count = attempt + 1
                return detection_result
            except Exception as e:
                if attempt < self.config.max_retries:
                    # 指数退避重试
                    wait_time = 0.5 * (2**attempt)
                    logger.warning(
                        f"🔍 {self.name} 检测失败，重试中... "
                        f"[attempt={attempt+1}/{self.config.max_retries+1}] "
                        f"[wait={wait_time}s] [error={e!s}]"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # 最后一次重试也失败
                    logger.error(f"❌ {self.name} 检测失败，已达最大重试次数 [error={e!s}]")
                    raise AIServiceError(self.name, f"所有重试失败: {e!s}") from e

        # 不应该到这里（所有重试都失败）
        raise AIServiceError(self.name, "所有重试已耗尽")


# ============================================================================
# 备份服务商实现
# ============================================================================


class BackupAIServiceProvider(AIServiceProvider):
    """备份 AI 服务商"""

    def __init__(self):
        config = AIServiceConfig(
            enabled=settings.ai_spam_backup_enabled,
            api_key=settings.ai_spam_backup_api_key,
            api_base=settings.ai_spam_backup_api_base,
            model=settings.ai_spam_backup_model,
            temperature=settings.ai_spam_backup_temperature,
            threshold=settings.ai_spam_backup_threshold,
            timeout=settings.ai_spam_backup_timeout,
            max_retries=settings.ai_spam_backup_max_retries,
            max_length=settings.ai_spam_max_length,  # 共享最大长度配置
            client_idle_rebuild_minutes=settings.ai_spam_client_idle_rebuild_minutes,
            client_max_lifetime_hours=settings.ai_spam_client_max_lifetime_hours,
        )
        super().__init__("backup", config)

    @property
    def is_available(self) -> bool:
        """检查备份服务商是否可用"""
        return (
            self.config.enabled
            and bool(self.config.api_key)
            and settings.ai_spam_enabled  # 备份服务商需要主服务启用
        )

    async def detect(self, text: str, use_context_prompt: bool = False) -> AIDetectionResult:
        """检测文本是否为垃圾信息（带重试）"""
        # 文本长度限制
        if len(text) > self.config.max_length:
            logger.warning(
                f"文本过长，截断处理 [length={len(text)}] [max={self.config.max_length}]"
            )
            text = text[: self.config.max_length]

        # 带重试的 API 调用
        for attempt in range(self.config.max_retries + 1):
            try:
                result = await self._call_api(text, use_context_prompt)
                detection_result = self._process_result(result)
                detection_result.attempt_count = attempt + 1
                return detection_result
            except Exception as e:
                if attempt < self.config.max_retries:
                    # 指数退避重试
                    wait_time = 0.5 * (2**attempt)
                    logger.warning(
                        f"🔍 {self.name} 检测失败，重试中... "
                        f"[attempt={attempt+1}/{self.config.max_retries+1}] "
                        f"[wait={wait_time}s] [error={e!s}]"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # 最后一次重试也失败
                    logger.error(f"❌ {self.name} 检测失败，已达最大重试次数 [error={e!s}]")
                    raise AIServiceError(self.name, f"所有重试失败: {e!s}") from e

        # 不应该到这里（所有重试都失败）
        raise AIServiceError(self.name, "所有重试已耗尽")


# ============================================================================
# 混合 AI 检测器（协调器）
# ============================================================================


class HybridAIDetector:
    """混合 AI 检测器（自动回退）

    支持主备双服务商的智能切换和熔断保护
    """

    def __init__(
        self,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_minutes: int = 5,
    ):
        """初始化混合 AI 检测器

        Args:
            circuit_breaker_threshold: 熔断器阈值（连续失败次数）
            circuit_breaker_cooldown_minutes: 熔断器冷却时间（分钟）
        """
        self.primary = PrimaryAIServiceProvider()
        self.backup = BackupAIServiceProvider()
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_cooldown = timedelta(minutes=circuit_breaker_cooldown_minutes)
        self._stats: dict[str, AIServiceStats] = defaultdict(
            lambda: AIServiceStats(
                success_count=0,
                failure_count=0,
                consecutive_failures=0,
                last_failure_time=None,
                last_error="",
            )
        )

        # 记录初始化日志
        if self.primary.is_available:
            logger.info(
                f"✅ 主 AI 服务商已启用 [api_base={self.primary.config.api_base}] "
                f"[model={self.primary.config.model}] [threshold={self.primary.config.threshold}]"
            )
        else:
            logger.info("❌ 主 AI 服务商未启用或未配置")

        if self.backup.is_available:
            logger.info(
                f"✅ 备份 AI 服务商已启用 [api_base={self.backup.config.api_base}] "
                f"[model={self.backup.config.model}] [threshold={self.backup.config.threshold}]"
            )
        else:
            logger.debug("备份 AI 服务商未启用或未配置")

    def _is_circuit_open(self, provider: AIServiceProvider) -> bool:
        """检查熔断器是否打开

        Args:
            provider: AI 服务商

        Returns:
            True 如果熔断器打开（应跳过此服务商）
        """
        stats = self._stats[provider.name]

        # 检查连续失败次数
        if stats.consecutive_failures >= self.circuit_breaker_threshold:
            # 检查冷却时间
            if stats.last_failure_time:
                elapsed = datetime.now() - stats.last_failure_time
                if elapsed < self.circuit_breaker_cooldown:
                    logger.debug(
                        f"🔌 {provider.name} 熔断器打开，跳过 "
                        f"（剩余冷却时间: {self.circuit_breaker_cooldown - elapsed}）"
                    )
                    return True
                else:
                    # 冷却时间已过，重置熔断器
                    logger.debug(f"🔄 {provider.name} 熔断器冷却完成，重置")
                    provider.request_client_rebuild("cooldown_ended")
                    stats.reset_circuit()

        return False

    def _record_success(self, provider: AIServiceProvider) -> None:
        """记录成功

        Args:
            provider: AI 服务商
        """
        stats = self._stats[provider.name]
        stats.record_success()

    def _record_failure(self, provider: AIServiceProvider, error: str) -> None:
        """记录失败

        Args:
            provider: AI 服务商
            error: 错误信息
        """
        stats = self._stats[provider.name]
        stats.record_failure(error)

        # 标记 provider 需要重建 client
        provider.request_client_rebuild("provider_failure")

        # 检查是否触发熔断
        if stats.consecutive_failures >= self.circuit_breaker_threshold:
            provider.request_client_rebuild("circuit_breaker_tripped")
            logger.warning(
                f"⚠️ {provider.name} 触发熔断器 " f"（连续失败 {stats.consecutive_failures} 次）"
            )

    def _get_success_rate(self, provider_name: str) -> float:
        """计算服务商成功率

        Args:
            provider_name: 服务商名称

        Returns:
            成功率（0.0-1.0）
        """
        stats = self._stats[provider_name]
        total = stats.success_count + stats.failure_count
        if total == 0:
            return 0.0
        return stats.success_count / total

    async def detect(self, text: str) -> dict[str, Any]:
        """检测文本是否为垃圾信息（尝试所有服务商）

        Args:
            text: 待检测文本

        Returns:
            检测结果字典
        """
        # 尝试主服务商
        if self.primary.is_available and not self._is_circuit_open(self.primary):
            try:
                logger.debug(f"🔍 尝试使用 {self.primary.name} 服务商检测...")
                result = await self.primary.detect(text)
                self._record_success(self.primary)
                logger.info(
                    f"✅ {self.primary.name} 检测成功 [is_spam={result.is_spam}] "
                    f"[confidence={result.confidence:.2f}] "
                    f"[成功率:{self._get_success_rate(self.primary.name):.1%}] "
                    f"[{result.reasons[0] if result.reasons else "无原因"}]"
                )
                return {
                    "is_spam": result.is_spam,
                    "confidence": result.confidence,
                    "stage": result.stage,
                    "reasons": result.reasons,
                    "details": {
                        **result.details,
                        "provider": result.provider,
                        "attempt_count": result.attempt_count,
                    },
                }
            except AIServiceError as e:
                self._record_failure(self.primary, str(e))
                logger.warning(f"❌ {self.primary.name} 检测失败: {e}")
            except Exception as e:
                self._record_failure(self.primary, str(e))
                logger.error(f"💥 {self.primary.name} 发生意外错误: {e}")

        # 尝试备份服务商
        if self.backup.is_available and not self._is_circuit_open(self.backup):
            if self.primary.is_available:
                self.primary.request_client_rebuild("switching_to_backup")
                await self.primary.close()
            logger.info(f"🔄 切换到 {self.backup.name} 服务商...")
            try:
                logger.debug(f"🔍 尝试使用 {self.backup.name} 服务商检测...")
                result = await self.backup.detect(text)
                self._record_success(self.backup)
                logger.info(
                    f"✅ {self.backup.name} 检测成功 [is_spam={result.is_spam}] "
                    f"[confidence={result.confidence:.2f}] "
                    f"[成功率:{self._get_success_rate(self.backup.name):.1%}] "
                    f"[{result.reasons[0] if result.reasons else "无原因"}]"
                )
                return {
                    "is_spam": result.is_spam,
                    "confidence": result.confidence,
                    "stage": result.stage,
                    "reasons": result.reasons,
                    "details": {
                        **result.details,
                        "provider": result.provider,
                        "attempt_count": result.attempt_count,
                    },
                }
            except AIServiceError as e:
                self._record_failure(self.backup, str(e))
                logger.warning(f"❌ {self.backup.name} 检测失败: {e}")
            except Exception as e:
                self._record_failure(self.backup, str(e))
                logger.error(f"💥 {self.backup.name} 发生意外错误: {e}")

        # 所有服务商都失败
        primary_error = (
            self._stats[self.primary.name].last_error if self.primary.is_available else "未配置"
        )
        backup_error = (
            self._stats[self.backup.name].last_error if self.backup.is_available else "未配置"
        )
        logger.error(
            f"🚨 所有 AI 服务商都失败 " f"[primary: {primary_error}] [backup: {backup_error}]"
        )
        raise RuntimeError(f"AI 检测失败: primary={primary_error}, backup={backup_error}")

    async def detect_with_context(
        self, text: str, context_text: str | None = None
    ) -> dict[str, Any]:
        """带上下文的垃圾检测

        Args:
            text: 待检测文本
            context_text: 上下文文本（格式化后的对话上下文）

        Returns:
            检测结果字典
        """
        # 如果没有上下文，使用普通检测
        if not context_text:
            return await self.detect(text)

        # 文本长度限制（上下文 + 待检测文本）
        total_length = len(context_text)
        max_length = self.primary.config.max_length  # 使用主服务商的最大长度

        if total_length > max_length:
            # 截断上下文，保留待检测文本
            max_context_length = max_length - len(text) - 100  # 预留 100 字符缓冲
            if max_context_length > 0:
                context_text = context_text[:max_context_length] + "\n...\n【待检测消息】\n" + text
            else:
                # 上下文过长，放弃使用上下文
                logger.warning(f"上下文过长，使用普通检测 [total_length={total_length}]")
                return await self.detect(text)

        # 尝试主服务商
        if self.primary.is_available and not self._is_circuit_open(self.primary):
            try:
                logger.debug(f"🔍 尝试使用 {self.primary.name} 服务商检测（带上下文）...")
                result = await self.primary.detect(context_text, use_context_prompt=True)
                self._record_success(self.primary)
                logger.info(
                    f"✅ {self.primary.name} 检测成功 [is_spam={result.is_spam}] "
                    f"[confidence={result.confidence:.2f}] "
                    f"[成功率:{self._get_success_rate(self.primary.name):.1%}] "
                    f"[{result.reasons[0] if result.reasons else "无原因"}]"
                )
                return {
                    "is_spam": result.is_spam,
                    "confidence": result.confidence,
                    "stage": result.stage,
                    "reasons": result.reasons,
                    "details": {
                        **result.details,
                        "provider": result.provider,
                        "attempt_count": result.attempt_count,
                    },
                }
            except AIServiceError as e:
                self._record_failure(self.primary, str(e))
                logger.warning(f"❌ {self.primary.name} 检测失败: {e}")
            except Exception as e:
                self._record_failure(self.primary, str(e))
                logger.error(f"💥 {self.primary.name} 发生意外错误: {e}")

        # 尝试备份服务商
        if self.backup.is_available and not self._is_circuit_open(self.backup):
            if self.primary.is_available:
                self.primary.request_client_rebuild("switching_to_backup")
                await self.primary.close()
            logger.info(f"🔄 切换到 {self.backup.name} 服务商...")
            try:
                logger.debug(f"🔍 尝试使用 {self.backup.name} 服务商检测（带上下文）...")
                result = await self.backup.detect(context_text, use_context_prompt=True)
                self._record_success(self.backup)
                logger.info(
                    f"✅ {self.backup.name} 检测成功 [is_spam={result.is_spam}] "
                    f"[confidence={result.confidence:.2f}] "
                    f"[成功率:{self._get_success_rate(self.backup.name):.1%}] "
                    f"[{result.reasons[0] if result.reasons else "无原因"}]"
                )
                return {
                    "is_spam": result.is_spam,
                    "confidence": result.confidence,
                    "stage": result.stage,
                    "reasons": result.reasons,
                    "details": {
                        **result.details,
                        "provider": result.provider,
                        "attempt_count": result.attempt_count,
                    },
                }
            except AIServiceError as e:
                self._record_failure(self.backup, str(e))
                logger.warning(f"❌ {self.backup.name} 检测失败: {e}")
            except Exception as e:
                self._record_failure(self.backup, str(e))
                logger.error(f"💥 {self.backup.name} 发生意外错误: {e}")

        # 所有服务商都失败
        primary_error = (
            self._stats[self.primary.name].last_error if self.primary.is_available else "未配置"
        )
        backup_error = (
            self._stats[self.backup.name].last_error if self.backup.is_available else "未配置"
        )
        logger.error(
            f"🚨 所有 AI 服务商都失败 " f"[primary: {primary_error}] [backup: {backup_error}]"
        )
        raise RuntimeError(f"AI 上下文检测失败: primary={primary_error}, backup={backup_error}")

    async def close(self):
        """关闭所有服务商的 HTTP 客户端"""
        if self.primary.is_available:
            await self.primary.close()
        if self.backup.is_available:
            await self.backup.close()

    def get_stats(self) -> dict[str, dict]:
        """获取所有服务商的统计信息

        Returns:
            {provider_name: stats} 字典
        """
        providers = {"primary": self.primary, "backup": self.backup}
        result: dict[str, dict] = {}

        for name, provider in providers.items():
            if name not in self._stats:
                if provider.client is None and not provider._client_rebuild_pending:
                    continue
                stats = AIServiceStats()
            else:
                stats = self._stats[name]

            now = datetime.now()
            client_age_seconds = None
            client_idle_seconds = None
            if provider._client_created_at is not None:
                client_age_seconds = (now - provider._client_created_at).total_seconds()
            if provider._client_last_used_at is not None:
                client_idle_seconds = (now - provider._client_last_used_at).total_seconds()

            result[name] = {
                "success_count": stats.success_count,
                "failure_count": stats.failure_count,
                "consecutive_failures": stats.consecutive_failures,
                "last_failure_time": (
                    stats.last_failure_time.isoformat() if stats.last_failure_time else None
                ),
                "last_error": stats.last_error,
                "success_rate": self._get_success_rate(name),
                "client_initialized": provider.client is not None,
                "client_rebuild_pending": provider._client_rebuild_pending,
                "client_rebuild_reason": provider._client_rebuild_reason,
                "client_created_at": (
                    provider._client_created_at.isoformat()
                    if provider._client_created_at is not None
                    else None
                ),
                "client_last_used_at": (
                    provider._client_last_used_at.isoformat()
                    if provider._client_last_used_at is not None
                    else None
                ),
                "client_age_seconds": client_age_seconds,
                "client_idle_seconds": client_idle_seconds,
                "client_rebuild_count": provider._client_rebuild_count,
                "last_client_rebuild_at": (
                    provider._last_client_rebuild_at.isoformat()
                    if provider._last_client_rebuild_at is not None
                    else None
                ),
                "last_client_rebuild_reason": provider._last_client_rebuild_reason,
            }

        return result

    def reset_stats(self, provider_name: str | None = None) -> None:
        """重置统计信息

        Args:
            provider_name: 服务商名称，None 表示重置所有
        """
        if provider_name:
            if provider_name in self._stats:
                self._stats[provider_name] = AIServiceStats()
                logger.debug(f"🔄 {provider_name} 统计信息已重置")
        else:
            self._stats.clear()
            logger.debug("🔄 所有服务商统计信息已重置")


# ============================================================================
# 向后兼容包装（AISpamDetector）
# ============================================================================


class AISpamDetector:
    """AI 垃圾检测器（向后兼容包装）

    内部使用 HybridAIDetector 实现主备双服务商自动切换
    """

    def __init__(self) -> None:
        """初始化 AI 检测器（向后兼容）"""
        self._detector = HybridAIDetector()
        self.enabled = self._detector.primary.is_available

    async def detect(self, text: str) -> dict[str, Any]:
        """检测文本是否为垃圾信息

        Args:
            text: 待检测文本

        Returns:
            检测结果字典
        """
        # 如果未启用，直接返回不是垃圾
        if not self.enabled:
            return {
                "is_spam": False,
                "confidence": 0.0,
                "stage": "ai_api",
                "reasons": ["AI 检测未启用"],
                "details": {"enabled": False},
            }

        return await self._detector.detect(text)

    async def detect_with_context(
        self, text: str, context_text: str | None = None
    ) -> dict[str, Any]:
        """带上下文的垃圾检测

        Args:
            text: 待检测文本
            context_text: 上下文文本（格式化后的对话上下文）

        Returns:
            检测结果字典
        """
        # 如果未启用，直接返回不是垃圾
        if not self.enabled:
            return {
                "is_spam": False,
                "confidence": 0.0,
                "stage": "ai_api",
                "reasons": ["AI 检测未启用"],
                "details": {"enabled": False},
            }

        return await self._detector.detect_with_context(text, context_text)

    async def close(self):
        """关闭 HTTP 客户端"""
        await self._detector.close()


# ============================================================================
# 全局实例（延迟初始化）
# ============================================================================

_ai_detector: AISpamDetector | None = None


def get_ai_detector() -> AISpamDetector:
    """获取 AI 检测器全局实例（单例模式）"""
    global _ai_detector
    if _ai_detector is None:
        _ai_detector = AISpamDetector()
    return _ai_detector
