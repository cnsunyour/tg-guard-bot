"""沙盒任务执行核心：临时目录、子进程生命周期、输出采集与协议解码。

控制服务（server.py）不执行任何脚本代码——每个请求在这里变成一次性的
子进程任务：独立 tmpfs 目录 + start_new_session 进程组 + 有限输出读取 +
wall-clock 强杀。任务结束目录必删，不留可写跨任务状态。

安全定位：本模块与 harness 是纵深防御层。真正的安全边界是容器本身
（read_only + internal 网络 + cap_drop + 非 root），这里的隔离保证的是
「同一容器内的不同任务互不残留、失控任务被及时终止」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from sandbox.limits import LIMITS_BY_LANGUAGE, NODE_MAX_OLD_SPACE_MB, TaskLimits

# stdout 读取上限：read(MAX+1) 读到 MAX+1 字节即判定洪泛，无需等 EOF
STDOUT_LIMIT = 32 * 1024
STDERR_LIMIT = 16 * 1024
# wall-clock 在脚本执行预算之外追加的启动余量（解释器冷启动 + 读 task.json）
HARNESS_START_GRACE_SECONDS = 1.0

Language = str  # 合法值由 protocol.ExecuteRequest 校验（"python" | "javascript"）

_HARNESS_PY = Path(__file__).with_name("harness_python.py").resolve()
_HARNESS_JS = Path(__file__).with_name("harness_javascript.cjs").resolve()


class SandboxRunner:
    """顺序执行沙盒任务；并发槽位覆盖目录创建到清理的全生命周期。"""

    def __init__(
        self,
        *,
        concurrency: int = 2,
        tasks_root: str = "/tasks",
        limits_by_language: dict[str, TaskLimits | None] | None = None,
        stdout_limit: int = STDOUT_LIMIT,
        stderr_limit: int = STDERR_LIMIT,
        python_bin: str | None = None,
        node_bin: str = "node",
    ) -> None:
        self._tasks_root = tasks_root
        self._stdout_limit = stdout_limit
        self._stderr_limit = stderr_limit
        # 测试注入 None 可跳过 rlimit（见 sandbox/limits.py 模块注释）
        self._limits = (
            dict(LIMITS_BY_LANGUAGE) if limits_by_language is None else limits_by_language
        )
        # 解释器路径可注入：容器内用默认 PATH 解析即可；本地开发环境如 asdf
        # shim 在净化 env 下无法启动，测试需传真实二进制路径
        self._python_bin = python_bin or sys.executable
        self._node_bin = node_bin
        self._slots = asyncio.Semaphore(concurrency)
        self._active = 0

    def has_pending(self) -> bool:
        """是否有在飞任务（供优雅关闭等待）。"""
        return self._active > 0

    async def run(
        self,
        language: str,
        source: str,
        entry: str,
        context: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]:
        """执行一个任务，返回 ExecuteResponse 兼容的 dict（不含强类型校验）。

        信号量预检：并发已满时立即返回 busy，让排队请求快速失败（bot 侧
        按服务降级处理）。locked() 与 acquire() 之间存在窗口竞态，后果只是
        偶发排队而非并发超限，可接受。
        """
        if self._slots.locked():
            return _failure("busy", "并发槽位已满")
        await self._slots.acquire()
        self._active += 1
        try:
            return await self._run_once(language, source, entry, context, timeout_ms)
        finally:
            self._active -= 1
            self._slots.release()

    async def _run_once(
        self,
        language: str,
        source: str,
        entry: str,
        context: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]:
        task_dir: str | None = None
        proc: asyncio.subprocess.Process | None = None
        try:
            task_dir = tempfile.mkdtemp(dir=self._tasks_root, prefix="task-")
            task_json = str(Path(task_dir) / "task.json")
            payload: dict[str, Any] = {
                "source": source,
                "entry": entry,
                "context": context,
            }
            limits = self._limits.get(language)
            if limits is not None:
                # CPU 上限跟随本次请求的 wall-clock 上取整 + 1s 余量：正常路径
                # wall-clock 先到（分类 timeout），RLIMIT_CPU 只作内核级兜底——
                # 若固定 1s，短超时请求会先被 SIGXCPU 杀掉而误分类为 crash
                wall_seconds = timeout_ms / 1000 + HARNESS_START_GRACE_SECONDS
                cpu_floor = math.ceil(wall_seconds) + 1
                limits = replace(limits, cpu_seconds=max(limits.cpu_seconds, cpu_floor))
                payload["limits"] = limits.to_json()
            Path(task_json).write_text(
                json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")),
                encoding="utf-8",
            )
            if language == "javascript":
                # JS 无 exec 源码串的等价物，由 runner 落盘、harness 包装加载
                Path(task_dir, "script.cjs").write_text(source, encoding="utf-8")

            proc = await asyncio.create_subprocess_exec(
                *self._command(language, task_dir, task_json),
                cwd=task_dir,
                start_new_session=True,
                env={
                    # 环境白名单：不继承控制服务的任何变量（含可能的密钥）
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": task_dir,
                    "TMPDIR": task_dir,
                    "LANG": "C.UTF-8",
                },
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr, timed_out, overflow = await self._collect(proc, timeout_ms)
            if timed_out:
                return _failure("timeout", _to_text(stderr, self._stderr_limit))
            if overflow:
                return _failure("bad_output", "stdout 超出输出上限")
            return _decode(stdout, stderr, self._exit_code(proc), self._stderr_limit)
        except (TypeError, ValueError, UnicodeError, OSError) as exc:
            # 任务准备阶段失败（目录/写盘/spawn），与脚本行为无关
            return _failure("crash", f"task setup failed: {type(exc).__name__}")
        finally:
            if proc is not None and proc.returncode is None:
                kill_process_group(proc.pid)
                with contextlib.suppress(Exception):
                    await proc.wait()
            if task_dir is not None:
                shutil.rmtree(task_dir, ignore_errors=True)

    def _command(self, language: str, task_dir: str, task_json: str) -> list[str]:
        if language == "python":
            # -I：隔离模式，忽略环境变量/PYTHONPATH/user site
            return [self._python_bin, "-I", str(_HARNESS_PY), task_json]
        if language == "javascript":
            return [
                self._node_bin,
                f"--max-old-space-size={NODE_MAX_OLD_SPACE_MB}",
                str(_HARNESS_JS),
                task_dir,
            ]
        return []  # 语言合法性由协议层保证；这里空命令会立即失败

    async def _collect(
        self, proc: asyncio.subprocess.Process, timeout_ms: int
    ) -> tuple[bytes, bytes, bool, bool]:
        """并行读取 stdout/stderr 并等待退出，返回 (stdout, stderr, 超时?, 洪泛?)。

        三个等待项缺一不可：只 read 不 wait 会在杀进程后卡在管道 EOF，
        只 wait 不 read 会在管道写满后让子进程阻塞造成假死锁。
        """
        assert proc.stdout is not None and proc.stderr is not None
        stdout_task = asyncio.create_task(proc.stdout.read(self._stdout_limit + 1))
        # stderr 先截留前 16KB 供故障详情，之后持续排空——否则啰嗦脚本写满
        # 管道缓冲后会阻塞，把本应完成的任务拖成假超时
        stderr_task = asyncio.create_task(_read_with_drain(proc.stderr, self._stderr_limit))
        wait_task = asyncio.create_task(proc.wait())
        wall_seconds = timeout_ms / 1000 + HARNESS_START_GRACE_SECONDS
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wall_seconds

        stdout = b""
        stderr = b""
        timed_out = False
        overflow = False
        pending: set[asyncio.Task[Any]] = {stdout_task, stderr_task, wait_task}
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is stdout_task:
                    stdout = task.result()
                    overflow = len(stdout) > self._stdout_limit
                elif task is stderr_task:
                    stderr = task.result()
            if overflow:
                break

        if timed_out or overflow:
            # 先杀整个进程组再收尾：harness 可能已退出但脚本派生的子进程仍在
            kill_process_group(proc.pid)
        await wait_task
        # 超时/洪泛路径下读取任务可能未完成，一并收割避免悬挂
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        stdout = (
            stdout if stdout else (stdout_task.result() if not stdout_task.exception() else b"")
        )
        stderr = (
            stderr if stderr else (stderr_task.result() if not stderr_task.exception() else b"")
        )
        return stdout, stderr, timed_out, overflow

    @staticmethod
    def _exit_code(proc: asyncio.subprocess.Process) -> int:
        # ⚠️ 不能写 `proc.returncode or 0`：SIGKILL 退出的 -9 会被 or 吞成 0
        return proc.returncode if proc.returncode is not None else -1


def kill_process_group(pid: int) -> None:
    """SIGKILL 整个进程组；进程组已不存在时静默（正常退出竞态）。"""
    import signal as _signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, _signal.SIGKILL)


async def _read_with_drain(reader: asyncio.StreamReader, keep_bytes: int) -> bytes:
    """读 keep_bytes 字节后持续排空到 EOF（多余内容丢弃），防止管道背压。"""
    head = await reader.read(keep_bytes)
    if len(head) < keep_bytes:
        return head  # 已 EOF
    drain_size = 65536
    while await reader.read(drain_size):
        pass
    return head


def _to_text(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8", errors="replace")


def _failure(error: str, detail: str = "") -> dict[str, Any]:
    return {"ok": False, "result": None, "error": error, "detail": detail}


def _decode(stdout: bytes, stderr: bytes, exit_code: int, stderr_limit: int) -> dict[str, Any]:
    """把 harness 的 stdout/退出码解码为统一结果结构。"""
    if len(stdout) > STDOUT_LIMIT:
        return _failure("bad_output", "stdout 超出输出上限")
    if exit_code != 0:
        lowered = stderr.lower()
        # MemoryError 栈特征优先归类，便于管理侧区分资源类故障
        is_memory = b"memoryerror" in lowered or b"out of memory" in lowered
        return _failure("memory" if is_memory else "crash", _to_text(stderr, stderr_limit))
    try:
        payload = json.loads(
            stdout.decode("utf-8"),
            parse_constant=lambda v: (_ for _ in ()).throw(ValueError(f"非法 JSON 常量 {v}")),
        )
    except (ValueError, UnicodeDecodeError) as exc:
        return _failure("bad_output", f"协议输出不可解析: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return _failure("bad_output", "协议输出缺少布尔 ok 字段")
    if payload["ok"]:
        result = payload.get("result")
        if not isinstance(result, dict):
            return _failure("bad_output", "成功结果必须是对象")
        return {"ok": True, "result": result, "error": None, "detail": ""}
    error = payload.get("error")
    if not isinstance(error, str):
        return _failure("bad_output", "失败输出缺少 error 分类")
    detail = payload.get("detail", "")
    if not isinstance(detail, str):
        detail = ""
    # harness 已给出分类 detail（如异常类型名）；完整 traceback 只进容器
    # 日志（stderr），不随协议回传给客户端
    return {"ok": False, "result": None, "error": error, "detail": detail}
