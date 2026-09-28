# 沙盒执行服务

运行管理员自定义验证脚本（`ask` / `verify` 两入口）的独立容器服务。
**与 bot 主进程完全隔离**：独立镜像（`Dockerfile.sandbox`）、独立 internal
网络、只读 rootfs、无 Linux 能力、非 root 运行——脚本失控的影响半径被
限制在本容器内，且容器内也拿不到 Postgres / Redis / bot 凭据。

## 架构

```
bot ──POST /execute（Bearer + protocol_version）──▶ server.py（控制面，不执行脚本）
                                                      │ 每请求：
                                                      │  1. /tasks 下建 0700 随机子目录
                                                      │  2. 写 task.json（source/entry/context/limits）
                                                      ▼
                                          spawn 解释器 + harness（start_new_session）
                                                      │ harness 先自设 rlimit（limits.py）
                                                      ▼
                                          加载脚本 → 调 ask/verify(ctx) → JSON 写 fd 1
                                          ← stdout 有限读取（32KB 洪泛即杀）
                                          ← wall-clock 超时 killpg（整进程组）
                                                      │ 3. 任务结束删子目录（finally 保证）
```

## 文件职责

| 文件 | 职责 |
|------|------|
| `protocol.py` | 协议单一来源：请求/响应 pydantic 模型 + 严格 JSON 校验（bot 镜像与沙盒镜像各有一份副本，字段改动两处同步生效） |
| `limits.py` | 各语言单任务 rlimit 表（经 task.json 注入 harness；本地测试可注入 None） |
| `runner.py` | 任务生命周期：临时目录、子进程 spawn、有限读取、超时 killpg、并发信号量 |
| `harness_python.py` | Python harness：`python3 -I` 运行，rlimit + import 白名单 + stdout 劫持，最终结果 `os.write(1)` 直写 |
| `harness_javascript.cjs` | Node harness：`new Function` 包装加载（无 require）、console/stdout 劫持、Promise 拒绝，结果 `fs.writeSync(1)` |
| `server.py` | aiohttp 控制面：Bearer 认证、`/healthz`、`/execute`、结果强类型校验、优雅关闭 |

## 安全模型（与 SECURITY.md 保持同步）

- **边界**：容器（read_only + internal 网络 + cap_drop ALL + no-new-privileges +
  非 root）是唯一安全边界；harness/runner 的语言层与资源限制是纵深防御，
  用于抬高恶意脚本门槛与拦截低级错误
- **控制面不执行代码**：脚本只在一次性子进程内运行，控制服务进程内存中
  无任何脚本可达对象
- **跨任务隔离**：每任务独立 0700 tmpfs 子目录、结束即删；并发默认 2；
  任务以同一非特权 UID 运行且无 ptrace 能力（cap_drop ALL），互不可窥
- **残余边界**（如实记录）：internal 网络上宿主网关绑定的服务理论上可达；
  容器整体失守的爆炸半径 = uid 65534 的容器内权限。后续强化路径：
  gVisor / 独立执行 VM

## 本地开发

```bash
# 直接启动（任务目录用系统临时目录替代 /tasks）
SANDBOX_API_KEY=dev-key-32-chars-minimum-xxxxxx SANDBOX_TASKS_ROOT=$(mktemp -d) \
  python -m sandbox.server

# 测试
python -m pytest tests/test_sandbox_protocol.py tests/test_sandbox_runner.py
```

容器内 `/tasks` 由 compose tmpfs 提供（`noexec`，防落地可执行文件）。
