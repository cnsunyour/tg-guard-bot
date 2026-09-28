"""各语言沙盒任务的资源限制表。

设计：runner 把限制值写入 task.json 交给 harness 自设，而不是 harness
硬编码——本地跑集成测试时可以显式注入 None/保守值（rlimit 按 UID 全局
生效，测试进程若直接设 RLIMIT_NPROC=4 会影响开发者会话的全部进程）。
容器级总预算（mem_limit/pids_limit/read_only）由 compose 施加，这里
只定义单任务限额。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TaskLimits:
    """单任务 rlimit 集（task.json 中 limits 字段的形状）。"""

    as_bytes: int  # RLIMIT_AS：虚拟地址空间（非实际内存占用）
    cpu_seconds: int  # RLIMIT_CPU
    fsize_bytes: int  # RLIMIT_FSIZE：普通文件写入上限（0=禁写；不限制管道）
    nproc: int  # RLIMIT_NPROC：按真实 UID 统计的进程/线程总数

    def to_json(self) -> dict[str, int]:
        return {
            "as_bytes": self.as_bytes,
            "cpu_seconds": self.cpu_seconds,
            "fsize_bytes": self.fsize_bytes,
            "nproc": self.nproc,
        }


# Python：harness 启动后第一时间自设
PYTHON_LIMITS = TaskLimits(
    as_bytes=128 * 1024 * 1024,
    cpu_seconds=1,
    fsize_bytes=0,
    nproc=4,
)

# JavaScript：Node 无 setrlimit 接口。堆上限由 runner 以
# `node --max-old-space-size=32` 参数施加；虚拟地址空间（V8 会预留大块）
# 与进程/线程数由容器总预算兜底。None = task.json 不携带 limits 字段。
JAVASCRIPT_LIMITS: TaskLimits | None = None

LIMITS_BY_LANGUAGE: dict[str, TaskLimits | None] = {
    "python": PYTHON_LIMITS,
    "javascript": JAVASCRIPT_LIMITS,
}

# Node 进程实际堆预算（MB），与 runner 启动参数保持一致
NODE_MAX_OLD_SPACE_MB = 32
