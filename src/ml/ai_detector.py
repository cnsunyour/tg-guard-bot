"""AI API 垃圾检测模块 - 支持主备双服务商自动切换

架构设计：
- AIServiceProvider: 抽象基类（含文本 detect 与 Vision detect_image）
- PrimaryAIServiceProvider: 文本主服务商
- BackupAIServiceProvider: 文本备份服务商
- VisionServiceProvider: 多模态 Vision 服务商（图片/贴纸，独立配置）
- HybridAIDetector: 协调器（熔断器、统计追踪、自动回退）
- AISpamDetector: 向后兼容包装
"""

import asyncio
import base64
import json
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from src.core.config import settings

# ============================================================================
# Vision 支持工具（模型判定 + 图片编码）
# ============================================================================

# 多模态模型白名单（按 model 名正则判断）
_VISION_MODEL_PATTERNS = [
    re.compile(r"^gpt-4o", re.IGNORECASE),
    re.compile(r"^chatgpt-4o", re.IGNORECASE),
    re.compile(r"^gpt-4-turbo", re.IGNORECASE),
    re.compile(r"^gpt-4-vision", re.IGNORECASE),
    re.compile(r"^gpt-5", re.IGNORECASE),
    re.compile(r"^o1", re.IGNORECASE),
    re.compile(r"^claude-.*(sonnet|opus|haiku)", re.IGNORECASE),
    re.compile(r"^gemini-", re.IGNORECASE),
    re.compile(r"^qwen.*vl", re.IGNORECASE),
    re.compile(r"^qwen.*-omni", re.IGNORECASE),
    re.compile(r"^glm-.*v", re.IGNORECASE),
    re.compile(r"^step-.*v", re.IGNORECASE),
    re.compile(r"^yi-vl", re.IGNORECASE),
    re.compile(r"^internvl", re.IGNORECASE),
    re.compile(r"^llama-3\.2-.*vision", re.IGNORECASE),
    re.compile(r"^llama-4", re.IGNORECASE),
    re.compile(r"^pixtral", re.IGNORECASE),
    re.compile(r"^deepseek.*vl", re.IGNORECASE),
    re.compile(r"^grok-.*vision", re.IGNORECASE),
    re.compile(r"^kimi-k2\.", re.IGNORECASE),
    re.compile(r"^doubao-seed-2", re.IGNORECASE),
]


def _is_vision_model(model: str) -> bool:
    """判断模型名是否支持多模态视觉

    兼容带 provider 前缀的模型名（OpenRouter 等网关常见）：
    - ``openai/gpt-4o-mini`` → 取 ``gpt-4o-mini``
    - ``openrouter/anthropic/claude-3-5-sonnet`` → 取 ``claude-3-5-sonnet``
    判定忽略大小写。
    """
    if not model:
        return False
    # 只取最后一个 "/" 之后的模型名进行判定
    base_name = model.rsplit("/", 1)[-1].strip()
    if not base_name:
        return False
    return any(p.search(base_name) for p in _VISION_MODEL_PATTERNS)


_VISION_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


def _read_image_as_base64(image_path: str) -> tuple[str, str, int]:
    """读取图片并 base64 编码（同步，调用方需用 run_in_executor 包裹）

    Args:
        image_path: 图片文件路径

    Returns:
        (base64 字符串, MIME 类型, 原始字节数)

    Raises:
        FileNotFoundError: 文件不存在或不是文件
    """
    path_obj = Path(image_path).resolve()
    if not path_obj.exists() or not path_obj.is_file():
        raise FileNotFoundError(f"图片文件不存在: {image_path}")
    mime = _VISION_MIME_MAP.get(path_obj.suffix.lower(), "image/jpeg")
    with open(path_obj, "rb") as f:
        data = f.read()
    return base64.b64encode(data).decode("ascii"), mime, len(data)


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


class VisionUnsupportedError(Exception):
    """没有启用且支持 Vision 的 provider（模型名判定不支持、或都未启用）"""

    pass


class VisionAllFailedError(Exception):
    """所有支持 Vision 的 provider 调用都失败"""

    def __init__(self, primary_error: str = "", backup_error: str = ""):
        self.primary_error = primary_error
        self.backup_error = backup_error
        super().__init__(
            f"Vision 所有服务商都失败: primary={primary_error or '未参与'}, "
            f"backup={backup_error or '未参与'}"
        )


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

# System Prompt - Vision 垃圾检测（图片 + 可选 caption + 可选上下文）
SYSTEM_PROMPT_VISION = """你是垃圾信息检测助手。请识别图片内容（含文字、二维码、logo、版式），结合可选的图片文字说明（caption）判断该图片是否为垃圾信息。

严格按照以下 JSON 格式返回结果，不要返回任何其他内容：
{
  "is_spam": true 或 false,
  "confidence": 0.00-1.00 之间的数字（保留两位小数，"是垃圾"的概率）,
  "reason": "简短说明判断理由（一句话）",
  "extracted_text": "图片中全部可读文字的完整提取（含二维码解码、水印联系方式；无则空串）"
}

**confidence 语义**：始终表示"是垃圾"的置信度
- 判定为垃圾（is_spam=true）→ confidence 较高，如 0.90
- 判定为正常（is_spam=false）→ confidence 较低，如 0.10

重点识别信号：
- 二维码 + 文案（加群/加好友/领红包/扫码关注）
- 博彩网站截图、色情图片、成人服务广告
- 推广海报版式（大字号促销、联系方式水印、带链接的引流图）
- 品牌 logo 冒充（钓鱼截图）
- 诈骗引流（刷单、兼职、贷款、投资理财截图）

正常图片示例：日常分享、技术截图、表情包、宠物照片、风景照、UI 讨论截图等。

重要：只返回 JSON，不要返回其他任何内容。"""

# System Prompt - Vision + 群组对话上下文
SYSTEM_PROMPT_VISION_WITH_CONTEXT = """你是垃圾信息检测助手。结合图片内容、可选的图片文字说明（caption）和群组对话上下文，判断该图片是否为垃圾信息。

严格按照以下 JSON 格式返回结果，不要返回任何其他内容：
{
  "is_spam": true 或 false,
  "confidence": 0.00-1.00 之间的数字（保留两位小数，"是垃圾"的概率）,
  "reason": "简短说明判断理由（一句话）",
  "extracted_text": "图片中全部可读文字的完整提取（含二维码解码、水印联系方式；无则空串）"
}

**重要：请结合对话上下文进行判断**
- 如果提供了【对话回复链】，优先参考回复链判断图片是否为正常对话的一部分
- 如果提供了【群组最近对话】，参考群组讨论主题判断图片是否相关
- 示例：群里问"这个手机壳哪里买" → 回复淘宝截图 → 判断为正常回答而非垃圾

**confidence 语义**：始终表示"是垃圾"的置信度
- 判定为垃圾（is_spam=true）→ confidence 较高，如 0.90
- 判定为正常（is_spam=false）→ confidence 较低，如 0.10

重点识别信号：
- 二维码 + 文案（加群/加好友/领红包/扫码关注）
- 博彩网站截图、色情图片、成人服务广告
- 推广海报版式（大字号促销、联系方式水印、带链接的引流图）
- 品牌 logo 冒充（钓鱼截图）
- 诈骗引流（刷单、兼职、贷款、投资理财截图）

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
        if (
            self._client_rebuild_pending
            and self._client_rebuild_reason == "circuit_breaker_tripped"
        ):
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
                    now - self._client_last_used_at
                    if self._client_last_used_at is not None
                    else None
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

    @staticmethod
    def _format_error(e: Exception) -> str:
        """格式化异常信息，避免日志只出现空错误。"""

        def _truncate(message: str, limit: int = 200) -> str:
            normalized = re.sub(r"\s+", " ", message).strip()
            if len(normalized) > limit:
                return normalized[: limit - 3] + "..."
            return normalized

        if isinstance(e, AIServiceError):
            message = _truncate(e.message)
            if message:
                return f"{e.__class__.__name__} [provider={e.provider}] " f"[message={message}]"
            return f"{e.__class__.__name__} [provider={e.provider}]"

        error_type = e.__class__.__name__

        if isinstance(e, httpx.TimeoutException):
            timeout_phase = None
            if isinstance(e, httpx.ConnectTimeout):
                timeout_phase = "connect"
            elif isinstance(e, httpx.ReadTimeout):
                timeout_phase = "read"
            elif isinstance(e, httpx.WriteTimeout):
                timeout_phase = "write"
            elif isinstance(e, httpx.PoolTimeout):
                timeout_phase = "pool"

            parts = [error_type]
            if timeout_phase:
                parts.append(f"[phase={timeout_phase}]")

            message = _truncate(str(e))
            if message:
                parts.append(f"[message={message}]")
            return " ".join(parts)

        if isinstance(e, httpx.HTTPStatusError):
            parts = [error_type, f"[status_code={e.response.status_code}]"]
            try:
                response_text = _truncate(e.response.text)
            except Exception:
                response_text = ""
            if response_text:
                parts.append(f"[response={response_text}]")

            message = _truncate(str(e))
            if message:
                parts.append(f"[message={message}]")
            return " ".join(parts)

        message = _truncate(str(e))
        if message:
            return f"{error_type} [message={message}]"
        return error_type

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
        except httpx.TimeoutException as e:
            logger.warning(
                f"⏱️ {self.name} API 请求超时 "
                f"[timeout_seconds={self.config.timeout}] "
                f"[error={self._format_error(e)}]"
            )
            raise
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

    # ------------------------------------------------------------------
    # Vision 直判图片（多模态）
    # ------------------------------------------------------------------

    @property
    def supports_vision(self) -> bool:
        """该 provider 配置的模型是否支持多模态视觉"""
        return _is_vision_model(self.config.model)

    async def detect_image(
        self,
        image_b64: str,
        mime: str,
        *,
        caption: str | None = None,
        context_text: str | None = None,
    ) -> AIDetectionResult:
        """Vision 直判图片是否为垃圾（带重试）

        Args:
            image_b64: base64 编码的图片内容
            mime: 图片 MIME 类型（如 image/jpeg）
            caption: 图片自带的文字说明（可选）
            context_text: 格式化后的群组对话上下文（可选）

        Returns:
            AIDetectionResult，details 含 extracted_text

        Raises:
            AIServiceError: 所有重试失败
        """
        use_context = bool(context_text and context_text.strip())
        system_prompt = SYSTEM_PROMPT_VISION_WITH_CONTEXT if use_context else SYSTEM_PROMPT_VISION

        text_parts: list[str] = []
        if use_context:
            text_parts.append(f"【群组对话上下文】\n{context_text}")
        if caption:
            text_parts.append(f"【图片说明 / caption】\n{caption}")
        text_parts.append("请按约定的 JSON 格式返回对该图片的垃圾判定结果。")

        user_content = [
            {"type": "text", "text": "\n\n".join(text_parts)},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{image_b64}",
                    "detail": settings.ai_spam_vision_detail,
                },
            },
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        for attempt in range(self.config.max_retries + 1):
            try:
                raw = await self._call_api_vision(messages)
                detection = self._process_vision_result(raw)
                detection.attempt_count = attempt + 1
                return detection
            except Exception as e:
                formatted_error = self._format_error(e)
                if attempt < self.config.max_retries:
                    wait_time = 0.5 * (2**attempt)
                    logger.warning(
                        f"🖼️ {self.name} Vision 检测失败，重试中... "
                        f"[attempt={attempt+1}/{self.config.max_retries+1}] "
                        f"[wait={wait_time}s] [error={formatted_error}]"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"❌ {self.name} Vision 检测失败，已达最大重试次数 "
                        f"[error={formatted_error}]"
                    )
                    raise AIServiceError(
                        self.name, f"Vision 所有重试失败: {formatted_error}"
                    ) from e

        raise AIServiceError(self.name, "Vision 所有重试已耗尽")

    async def _call_api_vision(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """发起 Vision HTTP 请求，返回解析后的 JSON dict"""
        url = f"{self.config.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }

        # Vision 请求超时独立配置（图片 payload 大，通常比文本检测长）
        vision_timeout = max(settings.ai_spam_vision_timeout, self.config.timeout)

        client = await self._ensure_client()
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=vision_timeout)
            response.raise_for_status()
        except httpx.TimeoutException as e:
            logger.warning(
                f"⏱️ {self.name} Vision API 请求超时 "
                f"[timeout_seconds={vision_timeout}] "
                f"[error={self._format_error(e)}]"
            )
            raise
        finally:
            self._client_last_used_at = datetime.now()

        data = response.json()
        if "choices" not in data or len(data["choices"]) == 0:
            raise ValueError("Vision API 响应格式错误：缺少 choices")

        content = data["choices"][0].get("message", {}).get("content", "")
        if not content:
            raise ValueError("Vision API 响应内容为空")

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise ValueError(f"无法解析 Vision 响应为 JSON: {content[:200]}")

        if not isinstance(result, dict):
            raise ValueError(f"Vision 响应不是字典: {type(result)}")
        if "is_spam" not in result or "confidence" not in result:
            raise ValueError(f"Vision 响应缺少必需字段: {result}")

        return result

    def _process_vision_result(self, result: dict[str, Any]) -> AIDetectionResult:
        """将 Vision 响应转为 AIDetectionResult（含 extracted_text）"""
        import math

        raw_is_spam = result.get("is_spam", False)
        if isinstance(raw_is_spam, bool):
            is_spam = raw_is_spam
        elif isinstance(raw_is_spam, str):
            is_spam = raw_is_spam.lower() in ("true", "1", "yes")
        else:
            is_spam = bool(raw_is_spam)

        raw_confidence = result.get("confidence", 0.0)
        try:
            confidence = float(raw_confidence)
            if not math.isfinite(confidence):
                logger.warning(f"Vision 返回非法 confidence: {raw_confidence}，使用 0.0")
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError) as e:
            logger.warning(f"Vision 无效 confidence: {raw_confidence} ({e})，使用 0.0")
            confidence = 0.0

        raw_reason = result.get("reason", "无理由")
        reason = str(raw_reason) if raw_reason else "无理由"
        extracted_text = str(result.get("extracted_text", "") or "")

        final_is_spam = is_spam and confidence >= self.config.threshold

        return AIDetectionResult(
            is_spam=final_is_spam,
            confidence=confidence,
            stage="ai_vision",
            reasons=[reason] if reason else [],
            details={
                "raw_is_spam": is_spam,
                "raw_confidence": confidence,
                "threshold": self.config.threshold,
                "model": self.config.model,
                "extracted_text": extracted_text,
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
                formatted_error = self._format_error(e)
                if attempt < self.config.max_retries:
                    # 指数退避重试
                    wait_time = 0.5 * (2**attempt)
                    logger.warning(
                        f"🔍 {self.name} 检测失败，重试中... "
                        f"[attempt={attempt+1}/{self.config.max_retries+1}] "
                        f"[wait={wait_time}s] [timeout_seconds={self.config.timeout}] "
                        f"[error={formatted_error}]"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # 最后一次重试也失败
                    logger.error(
                        f"❌ {self.name} 检测失败，已达最大重试次数 "
                        f"[timeout_seconds={self.config.timeout}] [error={formatted_error}]"
                    )
                    raise AIServiceError(self.name, f"所有重试失败: {formatted_error}") from e

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
                formatted_error = self._format_error(e)
                if attempt < self.config.max_retries:
                    # 指数退避重试
                    wait_time = 0.5 * (2**attempt)
                    logger.warning(
                        f"🔍 {self.name} 检测失败，重试中... "
                        f"[attempt={attempt+1}/{self.config.max_retries+1}] "
                        f"[wait={wait_time}s] [timeout_seconds={self.config.timeout}] "
                        f"[error={formatted_error}]"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # 最后一次重试也失败
                    logger.error(
                        f"❌ {self.name} 检测失败，已达最大重试次数 "
                        f"[timeout_seconds={self.config.timeout}] [error={formatted_error}]"
                    )
                    raise AIServiceError(self.name, f"所有重试失败: {formatted_error}") from e

        # 不应该到这里（所有重试都失败）
        raise AIServiceError(self.name, "所有重试已耗尽")


# ============================================================================
# Vision 服务商实现（通用类，主/备共用，由 name + config 区分）
# ============================================================================


class VisionServiceProvider(AIServiceProvider):
    """Vision 多模态服务商（图片/贴纸直判）

    与文本 provider 彻底解耦：独立 model，key/base 由 config.py 的
    vision_*_effective computed property 完成留空回退后传入。
    复用基类的 detect_image / _call_api_vision / _process_vision_result /
    supports_vision 及 httpx 客户端生命周期管理。
    """

    @property
    def is_available(self) -> bool:
        """Vision 服务商是否可用：已启用 + 有 key + 模型支持多模态

        将多模态判定纳入可用性，使"启用了但配了纯文本 model"直接视为不可用，
        在 candidates 选择阶段就被排除（而非等到调用才发现）。
        """
        return self.config.enabled and bool(self.config.api_key) and self.supports_vision

    async def detect(self, text: str, use_context_prompt: bool = False) -> AIDetectionResult:
        """Vision provider 不参与文本检测（契约保护）

        HybridAIDetector 的文本路径只走 self.primary / self.backup，
        不会调用到此处；保留以满足抽象基类约束。
        """
        raise NotImplementedError(f"{self.name} 是 Vision 专用 provider，不支持文本 detect()")


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

        # Vision 主/备（独立配置；key/base 经 config computed 完成留空回退）
        self.vision_primary = VisionServiceProvider(
            "vision_primary",
            AIServiceConfig(
                enabled=settings.ai_spam_vision_enabled,
                api_key=settings.vision_api_key_effective,
                api_base=settings.vision_api_base_effective,
                model=settings.ai_spam_vision_model,
                threshold=settings.ai_spam_vision_threshold,
                timeout=settings.ai_spam_vision_timeout,
                max_retries=settings.ai_spam_vision_max_retries,
                max_length=settings.ai_spam_max_length,
                client_idle_rebuild_minutes=settings.ai_spam_client_idle_rebuild_minutes,
                client_max_lifetime_hours=settings.ai_spam_client_max_lifetime_hours,
            ),
        )
        self.vision_backup = VisionServiceProvider(
            "vision_backup",
            AIServiceConfig(
                # 备依赖主开关：Vision 主未启用则备也不启用
                enabled=settings.ai_spam_vision_backup_enabled and settings.ai_spam_vision_enabled,
                api_key=settings.vision_backup_api_key_effective,
                api_base=settings.vision_backup_api_base_effective,
                model=settings.ai_spam_vision_backup_model,
                threshold=settings.ai_spam_vision_backup_threshold,
                timeout=settings.ai_spam_vision_timeout,
                max_retries=settings.ai_spam_vision_backup_max_retries,
                max_length=settings.ai_spam_max_length,
                client_idle_rebuild_minutes=settings.ai_spam_client_idle_rebuild_minutes,
                client_max_lifetime_hours=settings.ai_spam_client_max_lifetime_hours,
            ),
        )
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

        # Vision 主/备初始化日志（含"启用但 model 非多模态"兜底告警）
        if self.vision_primary.is_available:
            logger.info(
                f"✅ Vision 主服务商已启用 [api_base={self.vision_primary.config.api_base}] "
                f"[model={self.vision_primary.config.model}] "
                f"[threshold={self.vision_primary.config.threshold}]"
            )
        elif settings.ai_spam_vision_enabled and not self.vision_primary.supports_vision:
            logger.warning(
                f"⚠️ Vision 已启用但主模型非多模态，Vision 将不可用 "
                f"[model={self.vision_primary.config.model}]"
            )
        else:
            logger.debug("Vision 主服务商未启用或未配置 key")

        if self.vision_backup.is_available:
            logger.info(
                f"✅ Vision 备份服务商已启用 [api_base={self.vision_backup.config.api_base}] "
                f"[model={self.vision_backup.config.model}] "
                f"[threshold={self.vision_backup.config.threshold}]"
            )
        else:
            logger.debug("Vision 备份服务商未启用或未配置")

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
                formatted_error = self.primary._format_error(e)
                self._record_failure(self.primary, formatted_error)
                logger.warning(f"❌ {self.primary.name} 检测失败: {formatted_error}")
            except Exception as e:
                formatted_error = self.primary._format_error(e)
                self._record_failure(self.primary, formatted_error)
                logger.error(f"💥 {self.primary.name} 发生意外错误: {formatted_error}")

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
                formatted_error = self.backup._format_error(e)
                self._record_failure(self.backup, formatted_error)
                logger.warning(f"❌ {self.backup.name} 检测失败: {formatted_error}")
            except Exception as e:
                formatted_error = self.backup._format_error(e)
                self._record_failure(self.backup, formatted_error)
                logger.error(f"💥 {self.backup.name} 发生意外错误: {formatted_error}")

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
                formatted_error = self.primary._format_error(e)
                self._record_failure(self.primary, formatted_error)
                logger.warning(f"❌ {self.primary.name} 检测失败: {formatted_error}")
            except Exception as e:
                formatted_error = self.primary._format_error(e)
                self._record_failure(self.primary, formatted_error)
                logger.error(f"💥 {self.primary.name} 发生意外错误: {formatted_error}")

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
                formatted_error = self.backup._format_error(e)
                self._record_failure(self.backup, formatted_error)
                logger.warning(f"❌ {self.backup.name} 检测失败: {formatted_error}")
            except Exception as e:
                formatted_error = self.backup._format_error(e)
                self._record_failure(self.backup, formatted_error)
                logger.error(f"💥 {self.backup.name} 发生意外错误: {formatted_error}")

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

    async def detect_image_with_context(
        self,
        image_b64: str,
        mime: str,
        *,
        caption: str | None = None,
        context_text: str | None = None,
    ) -> dict[str, Any]:
        """带上下文的 Vision 直判图片（主备回退）

        Args:
            image_b64: base64 编码的图片
            mime: 图片 MIME 类型
            caption: 图片说明（可选）
            context_text: 格式化后的群组对话上下文（可选）

        Returns:
            检测结果 dict（字段对齐 detect_with_context）

        Raises:
            VisionUnsupportedError: 所有启用的 provider 都不支持 vision
            VisionAllFailedError: 所有支持 vision 的 provider 调用都失败
        """
        candidates: list[AIServiceProvider] = []
        if self.vision_primary.is_available:
            candidates.append(self.vision_primary)
        if self.vision_backup.is_available:
            candidates.append(self.vision_backup)

        if not candidates:
            raise VisionUnsupportedError(
                "没有可用的 Vision provider"
                "（检查 AI_SPAM_VISION_ENABLED / ai_spam_vision_model 是否多模态 / key 是否配置或可回退）"
            )

        primary_error = ""
        backup_error = ""

        for provider in candidates:
            if self._is_circuit_open(provider):
                logger.debug(f"🔌 {provider.name} 熔断中，跳过 Vision 调用")
                continue

            # 切换到备份时主动关闭 Vision 主的连接（对齐文本 detect_with_context 行为）
            if provider is self.vision_backup and self.vision_primary.is_available:
                self.vision_primary.request_client_rebuild("switching_to_backup")
                await self.vision_primary.close()
                logger.info(f"🔄 Vision 切换到 {provider.name}...")

            try:
                logger.debug(f"🖼️ 尝试使用 {provider.name} Vision 检测...")
                result = await provider.detect_image(
                    image_b64,
                    mime,
                    caption=caption,
                    context_text=context_text,
                )
                self._record_success(provider)
                logger.info(
                    f"✅ {provider.name} Vision 检测成功 [is_spam={result.is_spam}] "
                    f"[confidence={result.confidence:.2f}] "
                    f"[成功率:{self._get_success_rate(provider.name):.1%}] "
                    f"[{result.reasons[0] if result.reasons else '无原因'}]"
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
                formatted_error = provider._format_error(e)
                self._record_failure(provider, formatted_error)
                logger.warning(f"❌ {provider.name} Vision 检测失败: {formatted_error}")
                if provider is self.vision_primary:
                    primary_error = formatted_error
                else:
                    backup_error = formatted_error
            except Exception as e:
                formatted_error = provider._format_error(e)
                self._record_failure(provider, formatted_error)
                logger.error(f"💥 {provider.name} Vision 意外错误: {formatted_error}")
                if provider is self.vision_primary:
                    primary_error = formatted_error
                else:
                    backup_error = formatted_error

        # 所有支持 vision 的 provider 都失败或都熔断
        raise VisionAllFailedError(primary_error, backup_error)

    async def close(self):
        """关闭所有服务商的 HTTP 客户端"""
        if self.primary.is_available:
            await self.primary.close()
        if self.backup.is_available:
            await self.backup.close()
        if self.vision_primary.is_available:
            await self.vision_primary.close()
        if self.vision_backup.is_available:
            await self.vision_backup.close()

    def get_stats(self) -> dict[str, dict]:
        """获取所有服务商的统计信息

        Returns:
            {provider_name: stats} 字典
        """
        providers = {
            "primary": self.primary,
            "backup": self.backup,
            "vision_primary": self.vision_primary,
            "vision_backup": self.vision_backup,
        }
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

    @property
    def vision_enabled(self) -> bool:
        """Vision 是否启用且至少有一家可用 provider（图片/贴纸检测的总开关）"""
        return (
            self._detector.vision_primary.is_available
            or self._detector.vision_backup.is_available
        )

    @property
    def any_vision_provider_available(self) -> bool:
        """是否至少有一家可用的 Vision provider（语义同 vision_enabled，保留旧名）"""
        return self.vision_enabled

    async def detect_image_with_context(
        self,
        image_b64: str,
        mime: str,
        *,
        caption: str | None = None,
        context_text: str | None = None,
    ) -> dict[str, Any]:
        """Vision 直判图片（带上下文 + 主备回退）

        Raises:
            VisionUnsupportedError: AI 未启用或没有支持 vision 的 provider
            VisionAllFailedError: 所有 vision provider 都失败
        """
        if not self.vision_enabled:
            raise VisionUnsupportedError("Vision 未启用或无可用 provider")
        return await self._detector.detect_image_with_context(
            image_b64, mime, caption=caption, context_text=context_text
        )

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
