"""沙盒任务执行核心的集成测试：真实子进程，不 mock subprocess。

本地（macOS）与容器内都可跑；limits 注入 None 跳过 rlimit（UID 级限制
会波及开发者会话，见 sandbox/limits.py 注释），容器内的资源边界验收
在部署检查单里用真实 compose 环境覆盖。
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox.limits import TaskLimits
from sandbox.runner import SandboxRunner

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _resolve_node_bin() -> str | None:
    """解析真实 node 二进制路径。

    本地开发环境（如 asdf）的 PATH shim 依赖环境变量，在 runner 的白名单
    净化 env 下无法启动；process.execPath 给出真实二进制，可独立运行。
    生产容器内 node 是普通路径下的二进制，默认 "node" 即可。
    """
    try:
        result = subprocess.run(
            ["node", "-p", "process.execPath"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


_NODE_BIN = os.environ.get("SANDBOX_TEST_NODE") or _resolve_node_bin() or "node"

_PY_ASK = """
def ask(ctx):
    return {"text": "1+1=?", "options": [{"text": "2", "value": "v2"}], "state": {"n": 1}}
"""

_PY_SPIN = """
def ask(ctx):
    while True:
        pass
"""

_PY_BAD_IMPORT = """
import socket

def ask(ctx):
    return {"text": "never"}
"""

_PY_FLOOD = """
def ask(ctx):
    # 脚本 print 已被 harness 重定向到 stderr；stdout 洪泛只能来自超大合法
    # 结构（如超长题面），验证 runner 的 32KB 输出上限兜底
    return {"text": "x" * (64 * 1024)}
"""

_JS_ASK = """
module.exports.ask = function (ctx) {
  return { text: "JS 1+1=?", options: [{ text: "2", value: "v2" }], state: { n: 1 } };
};
"""

_JS_PROMISE = """
module.exports.ask = function (ctx) {
  return Promise.resolve({ text: "async" });
};
"""

_JS_REQUIRE = """
const fs = require("fs");
module.exports.ask = function (ctx) {
  return { text: "never" };
};
"""

# 本地测试不设 rlimit（防波及开发者会话）；容器内由生产 LIMITS_BY_LANGUAGE 生效
_NO_RLIMITS: dict[str, TaskLimits | None] = {"python": None, "javascript": None}


def _runner(tmp_path: Path, **kwargs) -> SandboxRunner:
    defaults = {"node_bin": _NODE_BIN}
    defaults.update(kwargs)
    return SandboxRunner(tasks_root=str(tmp_path), limits_by_language=_NO_RLIMITS, **defaults)


class TestPythonTasks:
    async def test_normal_ask(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("python", _PY_ASK, "ask", {}, 2000)
        assert outcome["ok"] is True
        assert outcome["result"]["text"] == "1+1=?"
        assert outcome["result"]["options"][0]["value"] == "v2"

    async def test_verify_decision(self, tmp_path: Path):
        source = "def verify(ctx):\n    return {'decision': 'pass'}\n"
        outcome = await _runner(tmp_path).run("python", source, "verify", {}, 2000)
        assert outcome["ok"] is True
        assert outcome["result"]["decision"] == "pass"

    async def test_infinite_loop_times_out(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("python", _PY_SPIN, "ask", {}, 300)
        assert outcome["ok"] is False
        assert outcome["error"] == "timeout"

    async def test_forbidden_import_fails(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("python", _PY_BAD_IMPORT, "ask", {}, 2000)
        assert outcome["ok"] is False
        assert outcome["error"] in ("crash", "bad_output")

    async def test_stdout_flood_rejected(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("python", _PY_FLOOD, "ask", {}, 5000)
        assert outcome["ok"] is False
        assert outcome["error"] == "bad_output"

    async def test_missing_entry_fails(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run(
            "python", "def other(ctx):\n    return {}\n", "ask", {}, 2000
        )
        assert outcome["ok"] is False

    async def test_async_entry_rejected(self, tmp_path: Path):
        source = "async def ask(ctx):\n    return {'text': 'x'}\n"
        outcome = await _runner(tmp_path).run("python", source, "ask", {}, 2000)
        assert outcome["ok"] is False

    async def test_ctx_reaches_script(self, tmp_path: Path):
        source = "def ask(ctx):\n" "    return {'text': '你好 ' + ctx['user']['name']}\n"
        outcome = await _runner(tmp_path).run(
            "python", source, "ask", {"user": {"name": "测试"}}, 2000
        )
        assert outcome["ok"] is True
        assert outcome["result"]["text"] == "你好 测试"


class TestJavascriptTasks:
    async def test_normal_ask(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("javascript", _JS_ASK, "ask", {}, 2000)
        assert outcome["ok"] is True
        assert outcome["result"]["text"] == "JS 1+1=?"

    async def test_promise_rejected(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("javascript", _JS_PROMISE, "ask", {}, 2000)
        assert outcome["ok"] is False

    async def test_require_rejected(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("javascript", _JS_REQUIRE, "ask", {}, 2000)
        assert outcome["ok"] is False

    async def test_missing_entry_fails(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run(
            "javascript", "module.exports.other = function () {};", "ask", {}, 2000
        )
        assert outcome["ok"] is False


class TestRunnerLifecycle:
    async def test_task_dir_cleaned_up(self, tmp_path: Path):
        runner = _runner(tmp_path)
        outcome = await runner.run("python", _PY_ASK, "ask", {}, 2000)
        assert outcome["ok"] is True
        assert list(tmp_path.iterdir()) == [], "任务目录结束后必须清理"

    async def test_timeout_cleans_task_dir(self, tmp_path: Path):
        outcome = await _runner(tmp_path).run("python", _PY_SPIN, "ask", {}, 300)
        assert outcome["error"] == "timeout"
        assert list(tmp_path.iterdir()) == []

    async def test_busy_when_concurrency_exhausted(self, tmp_path: Path):
        runner = _runner(tmp_path, concurrency=1)
        first = asyncio.create_task(runner.run("python", _PY_SPIN, "ask", {}, 10_000))
        await asyncio.sleep(0.3)  # 等第一个任务占住唯一槽位
        second = await runner.run("python", _PY_ASK, "ask", {}, 2000)
        assert second["error"] == "busy"
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    async def test_unknown_language_fails_closed(self, tmp_path: Path):
        runner = SandboxRunner(
            tasks_root=str(tmp_path),
            limits_by_language={},
        )
        outcome = await runner.run("ruby", "puts 1", "ask", {}, 2000)
        assert outcome["ok"] is False


@pytest.mark.skipif(
    sys.platform == "darwin", reason="Darwin 的 RLIMIT_AS 语义不同，内存验收在 Linux 容器内做"
)
class TestMemoryLimit:
    async def test_memory_bomb_killed(self, tmp_path: Path):
        # 容器内：limits 注入生产值，128MB AS 下分配大块内存应被 MemoryError 终止
        source = (
            "def ask(ctx):\n"
            "    blob = bytearray(512 * 1024 * 1024)\n"
            "    return {'text': 'never'}\n"
        )
        runner = SandboxRunner(
            tasks_root=str(tmp_path),
            limits_by_language={
                "python": TaskLimits(
                    as_bytes=128 * 1024 * 1024, cpu_seconds=1, fsize_bytes=0, nproc=4
                ),
                "javascript": None,
            },
        )
        outcome = await runner.run("python", source, "ask", {}, 5000)
        assert outcome["ok"] is False
        assert outcome["error"] == "memory"
