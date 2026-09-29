"""沙盒执行控制服务：认证、协议解析、结果强类型校验。

定位：这是控制面，**不执行任何脚本代码**——所有脚本执行都经 runner
进入一次性子进程。控制面与执行面的这一分界意味着：即使某个脚本任务
失控，它也接触不到控制面的 HTTP 监听与内存。

启动方式：`python -m sandbox.server`（容器 WORKDIR /app）。
配置全部来自环境变量（容器内不挂 .env）：
- SANDBOX_API_KEY：Bearer 共享密钥，未设置时 /execute 拒绝一切请求
- SANDBOX_CONCURRENCY：并发槽位（默认 2）
- SANDBOX_PORT：监听端口（默认 8080）
- SANDBOX_TASKS_ROOT：任务临时目录（默认 /tasks；本地调试可指向系统临时目录）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from aiohttp import web

from sandbox.protocol import (
    EXECUTION_ERROR_VALUES,
    MAX_DETAIL_CHARS,
    MAX_REQUEST_BODY_BYTES,
    PROTOCOL_VERSION,
    AskResult,
    ExecuteRequest,
    ExecuteResponse,
    ExecutionError,
    VerifyResult,
    validate_json_value,
)
from sandbox.runner import SandboxRunner

_HEALTH_ROUTE = "/healthz"

# 环境变量读取集中在模块顶部，便于审查容器配置面
_API_KEY = os.environ.get("SANDBOX_API_KEY", "").strip()
_PORT = int(os.environ.get("SANDBOX_PORT", "8080"))
_CONCURRENCY = max(1, int(os.environ.get("SANDBOX_CONCURRENCY", "2")))
_TASKS_ROOT = os.environ.get("SANDBOX_TASKS_ROOT", "/tasks")


def _log(message: str) -> None:
    # 沙盒容器不依赖 loguru，stderr 直出即可被 docker logs 采集
    print(message, file=sys.stderr, flush=True)


def _protocol_error(message: str, *, status: int = 400) -> web.HTTPException:
    """构造带协议化 JSON 体的 HTTP 错误响应。

    message 可能含 pydantic 校验错误全文（超长字段名/值由请求方控制），截断到
    协议上限——否则这里的 ExecuteResponse 构造本身会抛校验异常冒泡为 500。
    """
    body = ExecuteResponse(
        ok=False, error="bad_input", detail=message[:MAX_DETAIL_CHARS]
    ).model_dump_json()
    exc_class = {
        401: web.HTTPUnauthorized,
        503: web.HTTPServiceUnavailable,
    }.get(status, web.HTTPBadRequest)
    return exc_class(text=body, content_type="application/json")


@web.middleware
async def _auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Bearer 认证。internal 网络不是单向访问控制，执行接口必须自带认证。"""
    if request.path != _HEALTH_ROUTE:
        if not _API_KEY:
            raise _protocol_error("服务未配置 SANDBOX_API_KEY，拒绝执行", status=503)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_API_KEY}":
            raise _protocol_error("认证失败", status=401)
    return await handler(request)


async def _healthz(_request: web.Request) -> web.Response:
    # 免认证：无敏感信息，供容器 healthcheck 与 bot 探活
    return web.json_response({"status": "ok", "protocol_version": PROTOCOL_VERSION})


async def _execute(request: web.Request) -> web.Response:
    runner: SandboxRunner = request.app["runner"]
    try:
        raw = await request.content.read(MAX_REQUEST_BODY_BYTES + 1)
    # 传输层异常统一按坏请求处理
    except Exception:
        raise _protocol_error("请求体读取失败") from None
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        raise _protocol_error("请求体超出大小限制")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _protocol_error("请求体不是合法 JSON") from None

    try:
        req = ExecuteRequest.model_validate(body)
    except ValueError as exc:
        raise _protocol_error(f"请求协议不合法: {exc}") from None

    outcome = await runner.run(
        language=req.language,
        source=req.source,
        entry=req.entry,
        context=req.context,
        timeout_ms=req.timeout_ms,
    )
    status = 503 if outcome.get("error") == "busy" else 200
    return web.json_response(_build_response(req, outcome).model_dump(), status=status)


def _build_response(req: ExecuteRequest, outcome: dict[str, Any]) -> ExecuteResponse:
    """把 runner 的原始结果升级为强类型响应；脚本返回不合规结构按 bad_output 拒绝。

    失败分支出口完全防御化：error/detail 的来源包括脚本经 harness stdout 间接
    控制的内容（stderr 片段可达 runner 采集上限 16KB，error 理论可为任意字符串），
    超出协议枚举/长度的一律归一化，绝不让 pydantic 校验异常冒泡为 HTTP 500。
    """
    if not outcome.get("ok"):
        raw_error = outcome.get("error")
        raw_detail = outcome.get("detail")
        error: ExecutionError = "bad_output"
        if isinstance(raw_error, str) and raw_error in EXECUTION_ERROR_VALUES:
            # frozenset 成员检查不会窄化 Literal 类型，协议枚举已验证故显式收窄
            error = cast("ExecutionError", raw_error)
        detail = raw_detail[:MAX_DETAIL_CHARS] if isinstance(raw_detail, str) else ""
        return ExecuteResponse(ok=False, error=error, detail=detail)
    try:
        result: AskResult | VerifyResult
        if req.entry == "ask":
            result = AskResult.model_validate(outcome["result"])
        else:
            result = VerifyResult.model_validate(outcome["result"])
        # 结果整体仍需过 JSON 深度/类型白名单（state 等自由字段）
        validate_json_value(outcome["result"])
    except ValueError as exc:
        _log(f"任务结果不合规: {exc}")
        # pydantic 校验错误全文可被脚本间接放大（超限字段名/值逐行累积），同样截断
        return ExecuteResponse(
            ok=False,
            error="bad_output",
            detail=f"脚本返回结构不合规: {exc}"[:MAX_DETAIL_CHARS],
        )
    return ExecuteResponse(ok=True, result=result)


def _create_app() -> web.Application:
    app = web.Application(
        middlewares=[_auth_middleware],
        client_max_size=MAX_REQUEST_BODY_BYTES,
    )
    app["runner"] = SandboxRunner(concurrency=_CONCURRENCY, tasks_root=_TASKS_ROOT)

    async def _graceful_shutdown(app: web.Application) -> AsyncIterator[None]:  # type: ignore[misc]
        # cleanup_ctx 生成器：yield 前是启动阶段，yield 后在关闭阶段执行
        yield
        runner: SandboxRunner = app["runner"]
        for _ in range(20):
            if not runner.has_pending():
                break
            await asyncio.sleep(0.1)

    app.cleanup_ctx.append(_graceful_shutdown)
    app.router.add_get(_HEALTH_ROUTE, _healthz)
    app.router.add_post("/execute", _execute)
    return app


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        _log(f"sandbox server listening on :{_PORT} (protocol v{PROTOCOL_VERSION})")
        web.run_app(_create_app(), host="0.0.0.0", port=_PORT, print=None, handle_signals=True)


if __name__ == "__main__":
    main()
