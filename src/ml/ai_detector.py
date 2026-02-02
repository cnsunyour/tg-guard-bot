"""AI API 垃圾检测模块 - 第三方 AI API 检测"""

import asyncio
import json
import re
from typing import Any

import httpx
from loguru import logger

from src.core.config import settings


class AISpamDetector:
    """AI API 垃圾检测器 - 支持 OpenAI 兼容接口"""

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

    def __init__(self):
        """初始化 AI 检测器"""
        self.enabled = settings.ai_spam_enabled
        self.api_key = settings.ai_spam_api_key
        self.api_base = settings.ai_spam_api_base.rstrip("/")
        self.model = settings.ai_spam_model
        self.temperature = settings.ai_spam_temperature
        self.threshold = settings.ai_spam_threshold
        self.timeout = settings.ai_spam_timeout
        self.max_retries = settings.ai_spam_max_retries
        self.max_length = settings.ai_spam_max_length

        # 创建 HTTP 客户端
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

        if self.enabled:
            logger.info(
                f"AI 垃圾检测器已启用 [api_base={self.api_base}] [model={self.model}] "
                f"[threshold={self.threshold}]"
            )
        else:
            logger.info("AI 垃圾检测器未启用")

    async def detect(self, text: str) -> dict[str, Any]:
        """检测文本是否为垃圾信息

        Args:
            text: 待检测文本

        Returns:
            检测结果字典:
            {
                "is_spam": bool,           # 是否为垃圾
                "confidence": float,       # 置信度 (0.0-1.0)
                "stage": str,              # 检测阶段 "ai_api"
                "reasons": list[str],      # 判断理由列表
                "details": dict            # 详细信息
            }
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

        # 文本长度限制
        if len(text) > self.max_length:
            logger.warning(f"文本过长，截断处理 [length={len(text)}] [max={self.max_length}]")
            text = text[: self.max_length]

        # 带重试的 API 调用
        for attempt in range(self.max_retries + 1):
            try:
                result = await self._call_api(text)
                return self._process_result(result)
            except Exception as e:
                if attempt < self.max_retries:
                    # 指数退避重试
                    wait_time = 0.5 * (2**attempt)
                    logger.warning(
                        f"AI 检测失败，重试中... [attempt={attempt+1}/{self.max_retries+1}] "
                        f"[wait={wait_time}s] [error={e!s}]"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # 最后一次重试也失败
                    logger.error(f"AI 检测失败，已达最大重试次数 [error={e!s}]")
                    # ❌ 不要返回 is_spam: False，而是抛出异常让上层处理
                    raise RuntimeError(f"AI 检测失败: {e!s}") from e

        # 不应该到这里（所有重试都失败）
        raise RuntimeError("AI 检测失败：所有重试已耗尽")

    async def _call_api(self, text: str) -> dict[str, Any]:
        """调用 OpenAI 兼容 API

        Args:
            text: 待检测文本

        Returns:
            API 响应 JSON

        Raises:
            httpx.HTTPError: HTTP 请求错误
            ValueError: 响应解析错误
        """
        # 构建请求
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }

        # 发送请求
        response = await self.client.post(url, json=payload, headers=headers)
        response.raise_for_status()

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
            # 尝试正则提取 JSON
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
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

    def _process_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """处理 API 响应，转换为统一格式

        Args:
            result: API 返回的 JSON 结果

        Returns:
            统一格式的检测结果
        """
        is_spam = bool(result.get("is_spam", False))
        confidence = float(result.get("confidence", 0.0))
        reason = result.get("reason", "无理由")

        # 根据阈值判断
        final_is_spam = is_spam and confidence >= self.threshold

        # 构建返回结果
        detection_result = {
            "is_spam": final_is_spam,
            "confidence": confidence,
            "stage": "ai_api",
            "reasons": [reason] if reason else [],
            "details": {
                "raw_is_spam": is_spam,
                "raw_confidence": confidence,
                "threshold": self.threshold,
                "model": self.model,
            },
        }

        # 记录日志
        logger.info(
            f"AI 检测完成 [is_spam={final_is_spam}] [confidence={confidence:.2f}] "
            f"[threshold={self.threshold}] [reason={reason}]"
        )

        return detection_result

    async def close(self):
        """关闭 HTTP 客户端"""
        await self.client.aclose()


# 全局实例（延迟初始化）
_ai_detector: AISpamDetector | None = None


def get_ai_detector() -> AISpamDetector:
    """获取 AI 检测器全局实例（单例模式）"""
    global _ai_detector
    if _ai_detector is None:
        _ai_detector = AISpamDetector()
    return _ai_detector
