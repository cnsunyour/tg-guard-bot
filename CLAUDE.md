# CLAUDE.md - Telegram Guard Bot 开发指南

## 📋 快速命令参考

### 开发环境
```bash
make dev-up          # 启动开发环境 (Docker Compose)
make dev-logs        # 查看实时日志
make dev-down        # 停止开发环境
make dev-restart     # 重启服务
```

### 代码质量
```bash
make format          # 格式化代码 (Ruff)
make lint            # 代码检查 (Ruff + Mypy)
make test            # 运行测试套件
make check           # 完整检查 (format + lint + test)
```

### 生产部署
```bash
make prod-build      # 构建生产镜像
make prod-up         # 启动生产环境
make prod-down       # 停止生产环境
make prod-logs       # 查看生产日志
```

### 数据库管理
```bash
make db-migrate      # 运行数据库迁移
make db-backup       # 备份数据库
make db-restore      # 恢复数据库
```

### 模型训练
```bash
make train-model     # 训练反垃圾 ML 模型
```

---

## 🏗️ 核心架构

### 分层架构

```
📱 Telegram Bot (aiogram 3.x)
    ↓
🎯 Handlers (事件/命令处理)
    ↓
⚙️ Services (业务逻辑层)
    ↓
💾 Repositories (数据访问层)
    ↓
🗄️ Models (SQLAlchemy ORM)
```

### 关键子系统

#### 1. 私聊验证系统 (Private Chat Verification)

**设计目标**: 避免群内验证消息轰炸

**核心流程**:
```
用户加入群组
    ↓
限制发言权限 (ChatPermissions)
    ↓
尝试私聊发送验证 (bot.send_message → user_id)
    ↓
    ├─ 成功 → 用户私聊完成验证 → 恢复权限 → 群内发送欢迎消息
    └─ 失败 (TelegramForbiddenError) → 用户未启动 Bot
           ↓
       共享引导消息机制 (Redis 去重)
```

**关键文件**: `src/bot/handlers/verification.py`

**核心函数**:
- `on_user_join()` - 处理用户加入事件
- `handle_verification_success()` - 验证成功处理
- `handle_verification_timeout()` - 验证超时处理
- `handle_user_not_started_bot()` - 未启动 Bot 处理

#### 2. 共享引导消息机制

**设计目标**: 30 秒内多用户未启动 Bot,只发送一条群内引导消息

**技术实现**:
```python
# Redis 键设计
verification_hint:{chat_id} = "message_id:{msg_id}"  # TTL: 30s

# TTL 延长逻辑
if not existing_hint:
    # 发送引导消息
    await redis.setex(hint_key, 30, f"message_id:{msg_id}")
else:
    # 延长 TTL 让后入群用户有足够时间
    await redis.expire(hint_key, 30)  # ✅ 重置为 30 秒
```

**效果**: 10 人同时未启动 Bot → 1 条引导消息 (减少 90% 群内消息)

**关键文件**: `src/bot/handlers/verification.py`

**核心函数**:
- `handle_user_not_started_bot()` - 引导消息发送与去重
- `delete_hint_message_after_delay()` - 支持 TTL 延长的延迟删除

#### 3. 多层反垃圾检测系统

**设计目标**: 高效准确识别垃圾消息，同时最大限度降低误判率

**完整检测流程**:
```
消息输入
    ↓
┌─────────────────────────────────────────────────────────┐
│ 传统三段检测 (并行执行)                                   │
├─────────────────────────────────────────────────────────┤
│ Stage 1: 规则引擎                                        │
│ - 关键词黑名单 (置信度 0.9)                              │
│ - URL/链接检测 (置信度 0.85)                             │
│ - 联系方式检测 (置信度 0.8)                              │
│ - 重复字符/Emoji刷屏 (置信度 0.65-0.7)                   │
│ - 性能: ~1ms, O(1)查表                                   │
├─────────────────────────────────────────────────────────┤
│ Stage 2: ML分类器 (TF-IDF + SVM)                        │
│ - 中文分词 (jieba)                                       │
│ - TF-IDF特征提取 (5000维)                                │
│ - LinearSVC二分类                                        │
│ - 性能: ~50-100ms, 捕获变体                              │
├─────────────────────────────────────────────────────────┤
│ Stage 3: Embedding语义分析 (bge-small-zh)               │
│ - 文本嵌入向量生成                                       │
│ - 与垃圾原型余弦相似度匹配                                │
│ - 性能: ~100-200ms, 语义理解                             │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ↓
┌─────────────────────────────────────────────────────────┐
│ AI上下文检测 (可选, 并行执行)                            │
│ - OpenAI兼容API (GPT-4o-mini/DeepSeek等)                │
│ - 结合群组对话上下文理解语境                              │
│ - 自动入库训练样本                                       │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ↓
┌─────────────────────────────────────────────────────────┐
│ 结果合并策略                                             │
│ 1. 传统检测为垃圾 → 使用传统结果                         │
│ 2. AI检测为垃圾 → 使用AI结果 + 自动入库                  │
│ 3. 都不是垃圾 → 使用传统结果                             │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ↓
┌─────────────────────────────────────────────────────────┐
│ 活跃度置信度调整 (降低误判，始终生效)                     │
│ - 高活跃度用户 (activity >= 10)                          │
│ - 对数公式: reduction = 0.05 × log2(activity / 10)      │
│ - 最大降低: 0.15 (15%)                                   │
│ - 调整后 < 阈值 → 改判为正常                             │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ↓
┌─────────────────────────────────────────────────────────┐
│ 上下文一致性调整 (降低误判) ⭐ 最后防线                   │
│ 1. 回复链相关性检测 (优先级最高)                         │
│    - 计算当前消息与被回复消息的语义相似度                 │
│    - 相似度 >= 0.5 → 降低20%置信度                       │
│ 2. 群组话题一致性检测                                    │
│    - 计算与最近10条消息的平均相似度                       │
│    - 相似度 >= 0.7 → 降低15%置信度                       │
│ 3. 累计调整                                              │
│    - 调整后 < 阈值 → 改判为正常消息                      │
│ ⚠️ 设计原则: 只降低不提高 (避免误判话题转移)             │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ↓
            最终判定
```

**关键特性**:
- ✅ **渐进式过滤**: Stage 1检测到垃圾直接返回，避免不必要的计算
- ✅ **并行执行**: 传统检测和AI检测并行，提高效率
- ✅ **多重保护**: 活跃度调整 + 上下文调整，双重降低误判
- ✅ **线程池优化**: CPU密集操作在线程池执行，不阻塞事件循环

**关键文件**:
- `src/services/spam_detector.py` - 检测服务协调器
- `src/services/context_service.py` - 上下文管理服务
- `src/ml/rule_engine.py` - 规则引擎 (Stage 1)
- `src/ml/classifier.py` - TF-IDF + SVM (Stage 2)
- `src/ml/embedder.py` - 语义嵌入 (Stage 3 + 上下文一致性)
- `src/ml/ai_detector.py` - AI检测器

**阈值配置** (`src/core/config.py`):
```python
# 传统三段检测阈值
spam_threshold_rule: float = 0.8       # 规则引擎
spam_threshold_ml: float = 0.7         # ML 分类器
spam_threshold_embedding: float = 0.75 # Embedding

# AI检测配置
ai_spam_enabled: bool = False          # 是否启用AI检测
ai_spam_threshold: float = 0.8         # AI置信度阈值

# 上下文检测配置
context_enabled: bool = False          # 是否启用上下文检测
context_consistency_enabled: bool = True  # 上下文一致性检测

# 上下文一致性阈值
context_high_similarity_threshold: float = 0.7  # 高相似度阈值
context_confidence_reduction: float = 0.15      # 置信度降低幅度
reply_similarity_threshold: float = 0.5         # 回复链相似度阈值
reply_confidence_reduction: float = 0.2         # 回复链置信度降低幅度
```

**效果示例**:

*场景1: 正常回复问题*
```
群组对话:
  用户A: 这个手机壳哪里买的？
  用户B: 淘宝搜 xxx → https://taobao.com/xxx

检测结果:
  - Stage 1: 垃圾 (链接) 置信度 0.85
  - 回复链相似度: 0.72 (高度相关)
  - 调整后置信度: 0.65 (降低 0.20)
  - 最终判定: 正常消息 ✅
```

*场景2: 突然发广告*
```
群组对话:
  用户A: 这个 Python 库怎么用？
  用户B: 看官方文档吧
  用户C: 加微信xxx，低价VPN

检测结果:
  - Stage 1: 垃圾 (关键词) 置信度 0.95
  - 上下文相似度: 0.12 (话题不相关)
  - 调整: 不降低置信度 (避免误判话题转移)
  - 最终判定: 垃圾消息 ❌
```

---

## 🔑 重要设计模式

### 1. Redis 使用模式

**集中式键名管理** (`src/core/redis.py`):
```python
class RedisKeys:
    @staticmethod
    def verification_pending(chat_id: int, user_id: int) -> str:
        return f"verification:{chat_id}:{user_id}"

    @staticmethod
    def verification_hint(chat_id: int) -> str:
        return f"verification_hint:{chat_id}"
```

**TTL 延长模式**:
```python
# 检查 → 延长 → 递归等待
remaining_ttl = await redis.ttl(hint_key)
if remaining_ttl > 0:
    asyncio.create_task(
        delete_hint_message_after_delay(..., remaining_ttl)
    )
    return
```

### 2. 异步任务模式

**非阻塞延迟操作**:
```python
# 启动后台任务,不等待完成
asyncio.create_task(
    handle_verification_timeout(bot, chat_id, user_id, msg_id, timeout)
)
```

**异常抑制**:
```python
# 使用 contextlib.suppress 代替 try-except-pass
with contextlib.suppress(Exception):
    await bot.delete_message(chat_id, message_id)
```

### 3. 中间件链模式

**按优先级注册** (`src/main.py`):
```python
# 1. 白名单检查 (最高优先级)
dp.message.middleware(WhitelistMiddleware())
dp.callback_query.middleware(WhitelistMiddleware())

# 2. 速率限制 (防 DoS)
dp.message.middleware(ThrottleMiddleware(rate_limit=3, time_window=1))
dp.callback_query.middleware(ThrottleMiddleware(rate_limit=5, time_window=1))

# 3. 自动删除命令消息
dp.message.middleware(AutoDeleteMiddleware(response_delay=30))
```

### 4. 路由器优先级

**按功能重要性注册** (`src/main.py`):
```python
dp.include_router(events.router)        # 系统事件 (最高优先级)
dp.include_router(start.router)         # 启动命令
dp.include_router(admin.router)         # 管理命令
dp.include_router(moderation.router)    # 群管理命令
dp.include_router(verification.router)  # 入群验证
dp.include_router(antispam.router)      # 反垃圾 (最低优先级,兜底)
```

---

## 📂 代码结构导航

### 核心模块职责

```
src/
├── bot/                      # Telegram 交互层
│   ├── handlers/             # 事件/命令处理器
│   │   ├── verification.py   # ⭐ 入群验证 (私聊 + 共享引导)
│   │   ├── start.py          # ⭐ /start 命令 (深链接处理)
│   │   ├── antispam.py       # 反垃圾消息处理
│   │   ├── moderation.py     # 群管理命令 (kick/mute/warn/ban)
│   │   └── admin.py          # 管理员配置命令
│   ├── middlewares/          # 中间件
│   │   ├── whitelist.py      # 白名单检查
│   │   ├── throttle.py       # 速率限制
│   │   └── auto_delete.py    # 自动删除命令消息
│   └── filters/              # 自定义过滤器
│
├── services/                 # 业务逻辑层
│   ├── verification.py       # 验证挑战生成与验证
│   ├── moderation.py         # 群管理服务
│   ├── context_service.py    # ⭐ 上下文管理服务
│   └── spam_detector.py      # ⭐ 反垃圾检测协调器
│
├── ml/                       # ML/AI 模块
│   ├── rule_engine.py        # ⭐ 规则引擎 (Stage 1)
│   ├── classifier.py         # ⭐ TF-IDF + SVM (Stage 2)
│   ├── embedder.py           # ⭐ bge-small-zh (Stage 3 + 上下文一致性)
│   ├── ai_detector.py        # ⭐ AI检测器 (文本 + Vision)
│   └── trainer.py            # 模型训练脚本
│
├── models/                   # SQLAlchemy ORM
│   ├── group.py              # 群组配置
│   ├── user.py               # 用户警告记录
│   ├── spam_sample.py        # 垃圾样本 (训练数据)
│   └── audit_log.py          # 操作审计日志
│
├── repositories/             # 数据访问层
│   ├── group_repo.py         # 群组 CRUD
│   ├── user_repo.py          # 用户 CRUD
│   └── spam_repo.py          # 垃圾样本 CRUD
│
└── core/                     # 核心配置
    ├── config.py             # ⭐ Pydantic Settings (环境变量)
    ├── database.py           # PostgreSQL 连接池
    ├── redis.py              # ⭐ Redis 连接 + 键名管理
    └── executor.py           # 线程池 (用于 CPU 密集任务)
```

### 重点文件

| 文件 | 职责 | 重要性 |
|------|------|--------|
| `src/bot/handlers/verification.py` | 私聊验证 + 共享引导消息 | ⭐⭐⭐ |
| `src/services/spam_detector.py` | 反垃圾多层检测协调 | ⭐⭐⭐ |
| `src/services/context_service.py` | 上下文管理 (消息缓存/回复链) | ⭐⭐⭐ |
| `src/ml/embedder.py` | Embedding + 上下文一致性检测 | ⭐⭐⭐ |
| `src/core/redis.py` | Redis 键名管理 + TTL 模式 | ⭐⭐⭐ |
| `src/core/config.py` | 全局配置 (阈值/超时/模型路径) | ⭐⭐ |
| `src/main.py` | 应用入口 + 中间件注册 | ⭐⭐ |
| `src/ml/rule_engine.py` | 第一阶段快速过滤 | ⭐⭐ |
| `src/ml/ai_detector.py` | AI上下文检测 | ⭐⭐ |

---

## 🔧 开发注意事项

### Git 分支工作流

**双分支模型**:
- **`main`**: 生产稳定版本,只接受 `dev` 合并,禁止直接开发
- **`dev`**: 开发主线,所有功能分支从此创建并合并回此

**标准流程**:
```bash
# 1. 创建功能分支
git checkout dev && git pull origin dev
git checkout -b feature/新功能名

# 2. 开发 + 提交
git add . && git commit -m "feat: 功能描述"

# 3. 合并到 dev
git checkout dev && git merge feature/新功能名 --no-ff
git branch -d feature/新功能名 && git push origin dev

# 4. 测试通过后发布到 main
git checkout main && git merge dev --no-ff -m "release: 版本描述"
git push origin main && git checkout dev
```

### 代码风格

- **格式化**: Ruff 自动格式化 (`make format`)
- **类型检查**: Mypy 严格模式
- **异常处理**: 优先使用 `contextlib.suppress(Exception)` 而非 `try-except-pass`
- **日志记录**: 使用 Loguru (`logger.info/warning/error`)

### 配置管理

所有配置通过环境变量管理 (`.env` 文件):
```bash
# Telegram
BOT_TOKEN=...
ADMIN_IDS=123,456,789

# Database
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=...
DB_NAME=tg_guard

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# 传统三段检测阈值
SPAM_THRESHOLD_RULE=0.8
SPAM_THRESHOLD_ML=0.7
SPAM_THRESHOLD_EMBEDDING=0.75

# AI检测配置
AI_SPAM_ENABLED=false
AI_SPAM_API_KEY=...
AI_SPAM_API_BASE=https://api.openai.com/v1
AI_SPAM_MODEL=gpt-4o-mini
AI_SPAM_CLIENT_IDLE_REBUILD_MINUTES=60   # AI HTTP client 空闲多久后下次使用自动重建
AI_SPAM_CLIENT_MAX_LIFETIME_HOURS=24     # AI HTTP client 最大存活多久后下次使用自动重建
AI_SPAM_THRESHOLD=0.8

# 上下文检测配置
CONTEXT_ENABLED=false              # 是否启用上下文检测（需要AI检测）
CONTEXT_MESSAGE_COUNT=10           # 群组上下文消息数量
CONTEXT_TTL_MINUTES=10             # 上下文缓存时间（分钟）
CONTEXT_REPLY_DEPTH=3              # 回复链最大追溯深度
CONTEXT_MAX_TEXT_LENGTH=200        # 单条消息最大文本长度

# 上下文一致性检测配置（降低误判）
CONTEXT_CONSISTENCY_ENABLED=true   # 推荐启用
CONTEXT_HIGH_SIMILARITY_THRESHOLD=0.7    # 高相似度阈值
CONTEXT_CONFIDENCE_REDUCTION=0.15        # 置信度降低幅度
REPLY_SIMILARITY_THRESHOLD=0.5           # 回复链相似度阈值
REPLY_CONFIDENCE_REDUCTION=0.2           # 回复链置信度降低幅度

# 活跃度系统配置
ACTIVITY_MAX_CONFIDENCE_REDUCTION=0.15   # 最大置信度减少值
ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD=0     # 跳过垃圾检测全局阈值

# 说明：
# - 群组可通过 /activity 命令控制是否限制非文本消息
# - 活跃度记录、置信度修正、检测豁免功能始终工作
# - 宵禁模式下的活跃度门槛继续生效

# 验证配置
VERIFICATION_TIMEOUT=120  # 私聊验证超时时间 (秒)
```

### 数据库迁移

使用 Alembic 管理迁移:
```bash
# 创建迁移
alembic revision --autogenerate -m "描述"

# 应用迁移
make db-migrate

# 回滚
alembic downgrade -1
```

---

## 🚀 部署架构

### Docker Compose 服务

```yaml
services:
  bot:           # Python Bot 主服务
  postgres:      # PostgreSQL 16 (配置/日志/样本)
  redis:         # Redis 7 (缓存/队列/TTL)
```

### 资源要求

| 配置 | 最低要求 | 推荐配置 |
|------|---------|---------|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1GB | 2GB |
| 存储 | 10GB | 20GB SSD |

### 监控日志

**日志文件**:
- `logs/bot_{date}.log` - 所有日志 (DEBUG+)
- `logs/error_{date}.log` - 错误日志 (ERROR+)
- `logs/bot_{date}.json` - JSON 格式 (用于分析)

**查看日志**:
```bash
make dev-logs        # 实时查看开发环境日志
make prod-logs       # 实时查看生产环境日志
tail -f logs/error_*.log  # 查看错误日志
```

---

## 📚 技术栈参考

| 组件 | 版本 | 文档 |
|------|------|------|
| Python | 3.12+ | https://docs.python.org/3.12/ |
| aiogram | 3.x | https://docs.aiogram.dev/ |
| PostgreSQL | 16 | https://www.postgresql.org/docs/16/ |
| Redis | 7 | https://redis.io/docs/ |
| SQLAlchemy | 2.0 | https://docs.sqlalchemy.org/en/20/ |
| Pydantic | 2.x | https://docs.pydantic.dev/latest/ |
| scikit-learn | 1.4+ | https://scikit-learn.org/stable/ |

---

## ⚠️ 常见陷阱

1. **编辑文件前必须先读取**: 使用 Edit 工具前必须先用 Read 工具读取文件
2. **Redis 键名使用统一管理**: 所有 Redis 键通过 `RedisKeys` 类生成,避免硬编码
3. **异步任务不要忘记 await**: `asyncio.create_task()` 用于后台任务,直接调用协程需要 `await`
4. **TelegramForbiddenError 需要捕获**: 私聊失败是正常情况,必须优雅处理
5. **数据库模型修改后需要迁移**: 修改 `models/*.py` 后必须运行 `alembic revision`
6. **代码提交前运行 `make check`**: 确保通过格式化、检查和测试
7. **上下文检测需要AI检测**: `CONTEXT_ENABLED` 需要 `AI_SPAM_ENABLED=true` 才能工作
8. **上下文一致性可独立使用**: `CONTEXT_CONSISTENCY_ENABLED` 即使没有AI也能通过Embedding工作

---

## 🎯 反垃圾检测最佳实践

### 1. 配置推荐

**基础配置** (无AI):
```bash
# 传统三段检测
SPAM_THRESHOLD_RULE=0.8
SPAM_THRESHOLD_ML=0.7
SPAM_THRESHOLD_EMBEDDING=0.75

# 活跃度系统
ACTIVITY_MAX_CONFIDENCE_REDUCTION=0.15   # 置信度修正最大降低值
ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD=0     # 跳过检测阈值（0=使用群组配置）

# 上下文一致性（推荐启用）
CONTEXT_CONSISTENCY_ENABLED=true
CONTEXT_HIGH_SIMILARITY_THRESHOLD=0.7
CONTEXT_CONFIDENCE_REDUCTION=0.15
```

**进阶配置** (含AI):
```bash
# 启用AI检测
AI_SPAM_ENABLED=true
AI_SPAM_API_KEY=sk-xxx
AI_SPAM_MODEL=gpt-4o-mini
AI_SPAM_CLIENT_IDLE_REBUILD_MINUTES=60
AI_SPAM_CLIENT_MAX_LIFETIME_HOURS=24
AI_SPAM_THRESHOLD=0.8

# 启用上下文检测
CONTEXT_ENABLED=true
CONTEXT_MESSAGE_COUNT=10
CONTEXT_TTL_MINUTES=10

# 上下文一致性
CONTEXT_CONSISTENCY_ENABLED=true
REPLY_SIMILARITY_THRESHOLD=0.5
REPLY_CONFIDENCE_REDUCTION=0.2
```

### 2. 阈值调优指南

**降低误判率** (更保守):
```bash
SPAM_THRESHOLD_RULE=0.85          # 提高规则引擎阈值
SPAM_THRESHOLD_ML=0.75            # 提高ML阈值
SPAM_THRESHOLD_EMBEDDING=0.80     # 提高Embedding阈值
CONTEXT_CONFIDENCE_REDUCTION=0.20 # 增加上下文调整幅度
```

**提高检测率** (更激进):
```bash
SPAM_THRESHOLD_RULE=0.75          # 降低规则引擎阈值
SPAM_THRESHOLD_ML=0.65            # 降低ML阈值
SPAM_THRESHOLD_EMBEDDING=0.70     # 降低Embedding阈值
CONTEXT_CONFIDENCE_REDUCTION=0.10 # 减少上下文调整幅度
```

### 3. 性能优化建议

**CPU密集操作优化**:
- ✅ 规则引擎、ML分类器、Embedding已在线程池执行
- ✅ 传统检测和AI检测并行执行
- ✅ 上下文一致性检测使用异步Embedding

**内存优化**:
- 上下文消息数量: 10条 (约1-2分钟对话)
- 上下文TTL: 10分钟 (自动清理不活跃群组)
- 单条消息最大长度: 200字符 (避免缓存冗余)

**Redis优化**:
- 使用Pipeline减少RTT (上下文记录: 3次调用→1次)
- 合理设置TTL避免内存泄漏
- 使用键名管理类统一管理

### 4. 监控指标

**关键指标**:
```python
# 检测准确率
- 误判率 (False Positive Rate)
- 漏检率 (False Negative Rate)
- 各阶段检测占比

# 性能指标
- Stage 1 平均耗时: ~1ms
- Stage 2 平均耗时: ~50-100ms
- Stage 3 平均耗时: ~100-200ms
- AI检测平均耗时: ~500-1000ms

# 上下文调整效果
- 改判为正常的消息数
- 回复链相关性命中率
- 群组话题一致性命中率
```

**日志分析**:
```bash
# 查看误判案例
grep "改判为正常消息" logs/bot_*.log

# 查看上下文调整效果
grep "上下文调整后置信度" logs/bot_*.log

# 查看各阶段检测分布
grep "Stage [1-3] 检测到垃圾" logs/bot_*.log | wc -l
```

---

**最后更新**: 2026-02-12
**版本**: v1.2.0
**适用于**: tg-guard-bot 多层反垃圾检测版本
