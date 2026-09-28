"""沙盒执行服务客户端（bot 侧）。

设计原则：
- **绝无本地执行回退路径**——所有失败都向上抛异常，由调用方决定降级
  （回落群组原有验证方式），bot 容器里不存在解释脚本的代码路径
- 结果做与沙盒服务端相同的强类型校验（sandbox.protocol 单一来源）：
  即使沙盒容器被攻破返回恶意结构，也止步于 pydantic 模型
- 健康探测结果带缓存，避免每次执行都付探活往返；缓存期内不重复探测
"""

from __future__ import annotations

import json
import time
from typing import Literal

import httpx
from loguru import logger

from sandbox.protocol import (
    PROTOCOL_VERSION,
    AskResult,
    ExecuteRequest,
    ExecuteResponse,
    VerifyResult,
)
from src.core.config import settings

SandboxLanguage = Literal["python", "javascript"]
SandboxEntry = Literal["ask", "verify"]

# /execute 的健康探测缓存
_UNHEALTHY_RETRY_SECONDS = 5.0
# 响应体读取上限：正常结果 ≤32KB（沙盒侧 MAX_OUTPUT_BYTES），留协议包装余量
MAX_RESPONSE_BYTES = 64 * 1024


class SandboxUnavailableError(Exception):
    """沙盒服务不可用（未配置/掉线/超时/排队满/版本不匹配）——按服务降级处理。"""


class SandboxProtocolError(Exception):
    """沙盒返回了不合规结果——按执行故障处理（不罚用户）。"""


class SandboxClient:
    """对 sandbox 容器的 HTTP 客户端；模块级单例经 :func:`get_sandbox_client` 获取。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._healthy_until = 0.0
        self._unhealthy_until = 0.0

    @property
    def configured(self) -> bool:
        return bool(settings.sandbox_api_url.strip()) and bool(settings.sandbox_api_key.strip())

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.sandbox_request_timeout_seconds, connect=2.0),
                limits=httpx.Limits(max_keepalive_connections=2, max_connections=4),
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _ensure_healthy(self) -> None:
        """带缓存的健康探测；负缓存期内直接快速失败。"""
        now = time.monotonic()
        if now < self._healthy_until:
            return
        if now < self._unhealthy_until:
            raise SandboxUnavailableError("沙盒服务近期探测失败")
        try:
            response = await self._get_client().get(
                f"{settings.sandbox_api_url.rstrip('/')}/healthz"
            )
            response.raise_for_status()
            version = int(response.json().get("protocol_version", -1))
        except Exception as exc:
            self._unhealthy_until = now + _UNHEALTHY_RETRY_SECONDS
            logger.warning(f"沙盒健康探测失败: {exc}")
            raise SandboxUnavailableError("沙盒服务健康探测失败") from exc
        if version != PROTOCOL_VERSION:
            self._unhealthy_until = now + _UNHEALTHY_RETRY_SECONDS
            raise SandboxUnavailableError(f"沙盒协议版本不匹配: {version} != {PROTOCOL_VERSION}")
        self._healthy_until = now + settings.sandbox_health_interval_seconds

    async def execute(
        self,
        language: SandboxLanguage,
        source: str,
        entry: SandboxEntry,
        context: dict[str, object],
        timeout_ms: int = 2000,
    ) -> AskResult | VerifyResult:
        """执行一次脚本入口调用，返回强类型结果。

        异常语义：连接/超时/busy/版本不符 → SandboxUnavailableError；
        结果结构不合规 → SandboxProtocolError。两者均不计用户答错。
        """
        if not self.configured:
            raise SandboxUnavailableError("沙盒服务未配置")
        await self._ensure_healthy()

        request = ExecuteRequest(
            protocol_version=PROTOCOL_VERSION,
            language=language,
            source=source,
            entry=entry,
            context=context,
            timeout_ms=timeout_ms,
        )
        try:
            async with self._get_client().stream(
                "POST",
                f"{settings.sandbox_api_url.rstrip('/')}/execute",
                json=request.model_dump(),
                headers={"Authorization": f"Bearer {settings.sandbox_api_key}"},
            ) as response:
                if response.status_code in (429, 503):
                    raise SandboxUnavailableError("沙盒并发已满/服务过载")
                if response.status_code != 200:
                    # 4xx 属协议/请求侧故障（版本漂移、参数错），与「服务不可用」
                    # 区分开，避免掩盖 bot 侧缺陷
                    detail = (await response.aread())[:200].decode("utf-8", errors="replace")
                    error_cls = (
                        SandboxUnavailableError
                        if response.status_code >= 500
                        else SandboxProtocolError
                    )
                    raise error_cls(f"沙盒返回 {response.status_code}: {detail}")
                # 响应体限量读取：即使沙盒容器被攻陷返回超大响应也不无界读入
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        raise SandboxProtocolError("沙盒响应超出大小限制")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            self._healthy_until = 0.0
            raise SandboxUnavailableError(f"沙盒请求失败: {exc}") from exc

        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise SandboxProtocolError(f"沙盒响应不是合法 JSON: {exc}") from exc

        try:
            payload = ExecuteResponse.model_validate(data)
        except ValueError as exc:
            raise SandboxProtocolError(f"沙盒响应不合规: {exc}") from exc

        if not payload.ok:
            # 沙盒内部错误分类：busy 属服务容量问题，其余（timeout/memory/crash/
            # bad_output）属脚本执行故障——对 bot 而言都是「本次执行失败」
            if payload.error == "busy":
                raise SandboxUnavailableError("沙盒并发已满")
            raise SandboxProtocolError(f"脚本执行失败[{payload.error}]: {payload.detail}")

        if payload.result is None:
            raise SandboxProtocolError("沙盒成功响应缺少结果")
        return payload.result

    async def execute_ask(
        self,
        language: SandboxLanguage,
        source: str,
        context: dict[str, object],
        timeout_ms: int = 2000,
    ) -> AskResult:
        result = await self.execute(language, source, "ask", context, timeout_ms)
        if not isinstance(result, AskResult):
            raise SandboxProtocolError("ask 入口返回了错误类型")
        return result

    async def execute_verify(
        self,
        language: SandboxLanguage,
        source: str,
        context: dict[str, object],
        timeout_ms: int = 2000,
    ) -> VerifyResult:
        result = await self.execute(language, source, "verify", context, timeout_ms)
        if not isinstance(result, VerifyResult):
            raise SandboxProtocolError("verify 入口返回了错误类型")
        return result


_sandbox_client: SandboxClient | None = None


def get_sandbox_client() -> SandboxClient:
    """全局单例（对齐 get_redis / CASService 的懒初始化模式）。"""
    global _sandbox_client
    if _sandbox_client is None:
        _sandbox_client = SandboxClient()
    return _sandbox_client


async def close_sandbox_client() -> None:
    global _sandbox_client
    if _sandbox_client is not None:
        await _sandbox_client.close()
        _sandbox_client = None
