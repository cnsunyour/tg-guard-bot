"""可信 Python harness：由 runner 以 `python3 -I` 启动，单任务单进程。

职责边界：设置资源上限 → 加载任务 → 受限执行脚本 → 严格 JSON 输出。
本文件属于**可信代码**，不含任何机密；脚本代码只在 `_execute` 的
compile/exec 点运行。

语言层限制（import allowlist、同步入口）是纵深防御，用于抬高恶意脚本
门槛与拦截低级错误，**不是安全边界**——真正的边界是容器与子进程。
本地调试（task.json 无 limits 字段）时跳过 rlimit，避免开发者会话
被 UID 级限制波及（见 sandbox/limits.py 注释）。
"""

from __future__ import annotations

import builtins
import contextlib
import inspect
import json
import os
import resource
import sys
import traceback
from typing import Any

# 与 bot 侧静态审查（src/services/script_review.py）共享同一 allowlist，
# 两处改动必须同步（此处运行时兜底，彼处上传时提前报错）。
_ALLOWED_MODULES = frozenset(
    {
        "math",
        "random",
        "re",
        "json",
        "string",
        "hashlib",
        "hmac",
        "base64",
        "unicodedata",
        "datetime",
        "calendar",
        "itertools",
        "functools",
        "collections",
        "heapq",
        "bisect",
        "textwrap",
        "difflib",
        "statistics",
        "decimal",
        "fractions",
        "copy",
        "operator",
    }
)

_ORIGINAL_IMPORT = builtins.__import__


def _apply_limits(limits: dict[str, int] | None) -> None:
    """设置单任务 rlimit。FSIZE 不限制管道写入，输出洪泛由父进程负责。"""
    if limits is None:
        return
    as_bytes = limits["as_bytes"]
    cpu = limits["cpu_seconds"]
    fsize = limits["fsize_bytes"]
    nproc = limits["nproc"]
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))


def _restricted_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    # 只按根模块名判定；相对导入（level>0）在无包上下文的脚本中无意义
    root = name.split(".", 1)[0]
    if level or root not in _ALLOWED_MODULES:
        raise ImportError(f"模块不在允许列表: {name}")
    return _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)


def _redirect_stdout() -> None:
    # 脚本 print 进入容器日志（stderr）；协议 fd 1 只由 _emit 写入
    sys.stdout = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")


def _emit(payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    os.write(1, encoded)


def _reject_constant(value: str) -> Any:
    raise ValueError(f"非法 JSON 常量: {value}")


def _load_task(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as stream:
        task = json.load(stream, parse_constant=_reject_constant)
    if (
        not isinstance(task, dict)
        or not isinstance(task.get("source"), str)
        or not isinstance(task.get("entry"), str)
        or not isinstance(task.get("context"), dict)
    ):
        raise ValueError("任务文件字段非法")
    limits = task.get("limits")
    if limits is not None and not isinstance(limits, dict):
        raise ValueError("limits 字段非法")
    return task


def _execute(task: dict[str, Any]) -> dict[str, Any]:
    entry = task["entry"]
    if entry not in {"ask", "verify"}:
        raise ValueError("entry 必须是 ask 或 verify")
    # 完整 builtins 保留（保证正常脚本可用性），仅替换 __import__ 做白名单；
    # 脚本经对象图反射越界访问的残余风险由容器边界兜底
    script_builtins = vars(builtins).copy()
    script_builtins["__import__"] = _restricted_import
    namespace: dict[str, Any] = {
        "__builtins__": script_builtins,
        "__name__": "__sandbox_script__",
        # 刻意不注入 __file__：脚本不应感知文件系统布局
    }
    exec(compile(task["source"], "<sandbox_script>", "exec"), namespace, namespace)
    function = namespace.get(entry)
    if (
        not callable(function)
        or inspect.iscoroutinefunction(function)
        or inspect.isasyncgenfunction(function)
    ):
        raise TypeError(f"入口 {entry} 必须是同步函数")
    result = function(task["context"])
    if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
        raise TypeError("不允许异步/生成器结果")
    if not isinstance(result, dict):
        raise TypeError("入口返回值必须是 dict")
    # allow_nan=False 同时拒绝 NaN/Infinity 与循环引用
    json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return result


def main() -> None:
    # 先做一次极简读取尽早拿到 limits，让 rlimit 覆盖后续一切（含完整加载）
    with contextlib.suppress(Exception):
        _apply_limits(_peek_limits(sys.argv[1]))
    try:
        _redirect_stdout()
        task = _load_task(sys.argv[1])
        _apply_limits(task.get("limits"))
        _emit({"ok": True, "result": _execute(task)})
    # 协议边界要求任何异常（含 KeyboardInterrupt）都转为结构化输出
    except BaseException as exc:
        with contextlib.suppress(Exception):
            traceback.print_exc(file=sys.stderr)
        # MemoryError 归资源类故障（bot 侧与 crash 同为不计答错，但分类供监控）
        error = "memory" if isinstance(exc, MemoryError) else "crash"
        with contextlib.suppress(Exception):
            # 栈可能含敏感路径，stdout 只回分类名；完整栈在容器日志（stderr）
            _emit({"ok": False, "error": error, "detail": type(exc).__name__})


def _peek_limits(path: str) -> dict[str, int] | None:
    """极简读取：在完整校验前先拿到 limits，确保 rlimit 尽早生效。"""
    try:
        with open(path, encoding="utf-8") as stream:
            task = json.load(stream)
    except (OSError, ValueError):
        return None
    limits = task.get("limits") if isinstance(task, dict) else None
    return limits if isinstance(limits, dict) else None


if __name__ == "__main__":
    main()
