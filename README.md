<p align="center">
  <img src="assets/logo/banner-horizontal.svg" alt="Telegram Guard Bot" width="800"/>
</p>

# Telegram Guard Bot

一个功能强大的 Telegram 群管理机器人，支持入群验证、群管理和智能反垃圾功能。

[![Version](https://img.shields.io/github/v/tag/cnsunyour/tg-guard-bot?label=version&color=blue)](https://github.com/cnsunyour/tg-guard-bot/tags)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-brightgreen.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## ✨ 核心特性

### 🔐 入群验证
- **13 种验证方式**：
  - **内置验证**（7 种）：
    - 数学验证（四则运算，最多两步）
    - 滑块验证（点击绿色方块）
    - 问答验证（28 道常识题库）
    - 表情验证（50 组语义映射）
    - 图片验证码（扭曲文字识别）
    - 蜜罐验证（诱饵按钮检测）
    - 拼图验证（图片拼图）
  - **外部 CAPTCHA**（5 种）：
    - 🔐 Turnstile - Cloudflare 无感验证
    - 🤝 Friendly Captcha - 隐私友好，支持多 key 轮换
    - 🖼️ hCaptcha - 图片验证
    - 🔒 MTCaptcha - 自适应无感验证
    - ⚡ ALTCHA - 开源 Proof-of-Work 验证（自托管）
  - 🎲 随机验证（自动选择上述已启用的类型）
- **私聊验证系统**：避免群内验证消息轰炸，验证在私聊中完成
- **共享引导消息机制**：30秒内多用户未启动 Bot，只发送一条群内引导消息（减少 90% 群内消息）
- **可配置超时**：自定义验证时长（默认 120 秒，范围 30-300 秒）
- **自动处理**：超时或失败自动踢出并封禁 1 小时
- **统一 WebApp**：所有外部 CAPTCHA 使用统一 Telegram WebApp 界面

### 👮 群管理
- **踢人** `/kick` - 移出群组
- **禁言** `/mute` - 限制发言（支持时长：30m/2h/1d/永久）
- **解除禁言** `/unmute`
- **封禁** `/ban` - 永久封禁
- **解除封禁** `/unban`
- **警告** `/warn` - 累计警告（3次自动禁言24小时）
- **查看警告** `/warnings`
- **清除警告** `/clearwarnings`
- **清理不活跃用户** `/cleanup` - 清理已删除账号和很久不上线的用户（安全模式）
  - 支持预览、执行、分类清理
  - Redis 缓存成员列表（1小时 TTL）
  - 使用 Telethon 流式处理，支持 10 万+成员的超大群组

### 🚨 举报系统
- **用户举报** `/spam` 或 `/report` - 普通用户举报垃圾消息（管理员审核）
- **管理员处理** `/spam` - 管理员直接封禁并加入训练库
- **查看举报** `/reports` - 管理员查看待处理举报列表
- **审核举报** `/approve <id>` - 管理员批准举报并执行封禁
- **拒绝举报** `/reject <id>` - 管理员拒绝举报
- **误判反馈** `/notspam` - 标记非垃圾，帮助优化模型
- **防滥用限流** - 用户每天最多举报 10 次
- **消息删除** - 回复消息执行处罚时自动删除违规消息

### 🛡️ 智能反垃圾（多层检测系统）

#### 传统三段检测
- **Stage 1: 高级正则规则引擎** - 快速过滤关键词、链接、联系方式（~1ms，O(1)查表）
  - **多关键词联合检测**：使用前瞻断言实现复杂模式匹配
  - **Unicode 混淆检测**：识别繁简体、同义词等变体
  - **置信度分级**：CRITICAL(0.95) / HIGH(0.88) / MEDIUM(0.80) / LOW(0.70)
  - **自定义规则**：支持 JSON 配置文件扩展规则
  - 关键词黑名单（置信度 0.9）
  - URL/链接检测（置信度 0.85）
  - 联系方式检测（置信度 0.8）
  - 重复字符/Emoji刷屏（置信度 0.65-0.7）
- **Stage 2: ML 分类器** - TF-IDF + SVM 捕获变体（~50-100ms）
  - 中文分词（jieba）
  - TF-IDF特征提取（5000维）
  - LinearSVC二分类
- **Stage 3: 语义分析** - bge-small-zh-v1.5 Embedding（~100-200ms）
  - 文本嵌入向量生成
  - 与垃圾原型余弦相似度匹配

#### AI上下文检测（可选）
- **OpenAI兼容API** - 支持 GPT-4o-mini、DeepSeek、Moonshot 等
- **上下文理解** - 结合群组对话上下文判断语境
- **自动训练** - AI检测结果自动入库作为训练样本

#### 活跃度系统
- **非文本消息限制**（可选，群主可通过 `/activity` 控制）：
  - 启用时：活跃度 ≤ 0 的用户无法发送图片、贴纸、视频等
  - 禁用时：新用户也可自由发送非文本消息
- **活跃度规则**：
  - 普通文本消息：+1 活跃度
  - 非文本消息（图片/贴纸等）：不扣分
  - 外部转发/带链接消息：按”特殊非文本”处理
  - 每日无消息自动衰减 -1（活跃度 < 10 时）
- **辅助功能**（始终生效）：
  - **置信度修正**：活跃度越高，垃圾检测误判率越低
    - 对数公式：reduction = 0.05 × log2(activity / 10)
    - 最大降低 15% 置信度
  - **检测豁免**：高活跃度用户可跳过垃圾检测（阈值支持全局/群组配置）
  - **宵禁门槛**：宵禁期间根据活跃度控制发言权限

#### 上下文一致性检测（降低误判）⭐
- **回复链相关性检测**（优先级最高）：
  - 计算当前消息与被回复消息的语义相似度
  - 相似度 ≥ 0.5 → 降低 20% 置信度
- **群组话题一致性检测**：
  - 计算与最近10条消息的平均相似度
  - 相似度 ≥ 0.7 → 降低 15% 置信度
- **设计原则**：只降低不提高（避免误判话题转移）
- **效果**：即使规则引擎误判，上下文调整也能救回正常对话

#### 其他功能
- **编辑消息检测** - 应对先发普通消息后编辑成垃圾的手段
- **AI Vision 多模态检测** - 图片/贴纸直接送 AI 判垃圾（独立配置，主备双服务商）
  - 文本和图片可使用不同模型（成本优化）
  - 支持主备双服务商自动回退
  - key/base 可留空回退文本配置
- **文本长度预过滤** - 过滤过短或过长的异常消息，减少无效检测
- **管理员反馈** - 误判纠正，持续优化
- **自动模型训练** - 达到阈值自动触发训练

### ⚡ 其他功能
- **群组白名单** - 只在授权群组中提供服务，自动退出未授权群组
- **反频道马甲** - 禁止用户以频道身份发言，避免广告滥用
- **消息删除工具** - 批量删除消息（delbefore/delafter/delrange）
- **健康监控** `/health` - 系统状态和性能指标
- **统计信息** `/stats` - 反垃圾统计和运行信息
- **自动备份** - 数据库定时备份
- **日志轮转** - 自动压缩和清理日志

## 📊 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.12+ | 异步编程 |
| aiogram | 3.6+ | Telegram Bot 框架 |
| Telethon | 1.42+ | Telegram Client API（用于大群成员管理） |
| PostgreSQL | 16 | 主数据库 |
| Redis | 7 | 缓存和队列 |
| SQLAlchemy | 2.0+ | ORM |
| scikit-learn | 1.4+ | ML 分类器 |
| fastembed | 0.3+ | 语义嵌入 |
| jieba | 0.42+ | 中文分词 |
| lottie/cairosvg | - | TGS 动画贴纸渲染 |

## 🚀 快速开始

### 前置要求
- Docker 和 Docker Compose
- Telegram Bot Token（从 [@BotFather](https://t.me/botfather) 获取）
- 你的 Telegram User ID（从 [@userinfobot](https://t.me/userinfobot) 获取）

### 1. 克隆项目

```bash
git clone https://github.com/cnsunyour/tg-guard-bot.git
cd tg-guard-bot
```

### 2. 配置环境变量

```bash
cp .env.example .env
nano .env  # 或使用其他编辑器
```

**必填配置**：
```env
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_IDS=123456789
MODEL_SIGNATURE_KEY=please_set_a_random_secret_at_least_32_chars
# 生产环境还必须设置 REDIS_PASSWORD（否则启动会被拒绝）
# DB_PASSWORD 在生产环境也不能使用默认值 postgres
```

### 3. 启动服务

使用 Make（推荐）：
```bash
make dev-up        # 启动开发环境
make dev-logs      # 查看日志
```

或使用 Docker Compose：
```bash
docker compose up -d
docker compose logs -f bot
```

### 4. 初始化数据库

```bash
make db-migrate
```

### 5. 训练反垃圾模型

```bash
make train-samples  # 添加示例数据
make train-model    # 训练模型
```

### 6. 测试 Bot

1. 将 Bot 添加到测试群组
2. 设为管理员（授予删除消息、封禁用户权限）
3. 发送 `/start` 测试

## 📖 文档

| 文档 | 说明 |
|------|------|
| [QUICKSTART.md](QUICKSTART.md) | 详细的快速开始指南 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | 生产环境部署指南 |
| [SECURITY.md](SECURITY.md) | 安全说明与建议 |
| [docs/backup-strategy.md](docs/backup-strategy.md) | 备份策略说明 |
| [captcha-webapp/README.md](captcha-webapp/README.md) | 统一 CAPTCHA WebApp 部署指南 |
| [altcha-backend/README.md](altcha-backend/README.md) | ALTCHA PHP 后端部署指南 |

## 🔧 Make 命令

### 开发环境
```bash
make dev-up          # 启动开发环境
make dev-down        # 停止开发环境
make dev-logs        # 查看日志
```

### 生产环境
```bash
make prod-build      # 构建生产镜像
make prod-up         # 启动生产环境
make prod-down       # 停止生产环境
make prod-restart    # 重启 Bot
make prod-logs       # 查看日志
```

### 数据库
```bash
make db-migrate      # 运行数据库迁移
make db-shell        # 进入数据库 Shell
```

### 模型训练
```bash
make train-samples   # 添加示例训练数据
make train-model     # 训练反垃圾模型
```

### 维护
```bash
make clean           # 清理临时文件
make clean-all       # 清理所有数据（危险）
make status          # 查看服务状态
make help            # 显示所有命令
```

### 备份与恢复（GFS 轮转策略）
```bash
make backup                              # 自动备份（PostgreSQL + Redis）
make backup-postgres                      # 仅备份 PostgreSQL
make backup-redis                         # 仅备份 Redis
make backup-list                          # 列出所有备份文件
make backup-cleanup                       # 清理过期备份
make backup-restore-postgres FILE=<文件>   # 恢复 PostgreSQL
make backup-restore-redis FILE=<文件>      # 恢复 Redis
make backup-setup-cron                    # 设置自动备份定时任务
```

## 🎮 Bot 命令

### 用户命令
- `/start` - 查看帮助信息
- `/help` - 查看帮助信息

### 管理员命令

**群组设置**
- `/groupset` - 群组设置统一入口（验证、反垃圾、活跃度等）
- `/setverify` - 设置验证方式
- `/settimeout` - 设置验证超时时间
- `/verifyconfig` - 查看验证配置

**成员管理**
- `/kick @user` 或 `/kick <user_id>` - 踢出成员（支持 @username 和 user_id）
- `/mute @user [时长]` - 禁言成员（支持：30m/2h/1d/永久）
- `/unmute @user` - 解除禁言
- `/ban @user` - 封禁成员
- `/unban @user` - 解除封禁
- `/warn @user [原因]` - 警告成员（3次自动禁言24小时）
- `/warnings @user` - 查看警告记录
- `/clearwarnings @user` - 清除警告

**消息管理**
- `/delbefore` - 回复某消息，删除该消息之前的所有消息
- `/delafter` - 回复某消息，删除该消息之后的所有消息
- `/delrange` - 回复两条消息，删除这两条消息之间的所有消息

**用户清理**
- `/cleanup` - 清理不活跃用户（安全模式）
  - `/cleanup` - 预览清理
  - `/cleanup run` - 执行清理（已删除 + 很久不上线）
  - `/cleanup deleted` - 仅清理已删除用户
  - `/cleanup inactive` - 仅清理很久不上线的用户
  - `/cleanup refresh` - 强制刷新缓存
  - `/cleanup cache` - 查看缓存状态

**举报与反垃圾**
- `/spam` 或 `/report` - 举报/标记垃圾消息
  - 普通用户：创建举报，等待管理员审核
  - 管理员：直接封禁并加入训练库
- `/notspam`、`/nospam` 或 `/unspam` - 标记非垃圾（误判反馈，帮助优化模型）
  - 支持消息链接格式（t.me/c/xxx/xxx）
  - 自动删除旧正样本后添加负样本
- `/reports` - 查看待处理举报列表
- `/approve <id>` - 批准举报并执行封禁
- `/reject <id>` - 拒绝举报
- `/antispam` - 配置反垃圾（阈值、惩罚力度等）
- `/antichannel` - 配置反频道马甲（禁止频道身份发言）

**活跃度系统**
- `/activity` - 活跃度系统开关（启用/禁用）
- `/activityskip [阈值]` - 查看或设置活跃度跳过垃圾检测的阈值

### 超级管理员命令
- `/health` - 查看系统健康状态
- `/stats` - 查看统计信息
- `/whitelist` - 白名单管理
  - `/whitelist` - 列出所有白名单群组
  - `/whitelist add <chat_id> [群组名称]` - 添加群组到白名单
  - `/whitelist remove <chat_id>` - 从白名单移除群组

## 🔒 群组白名单

Bot 支持群组白名单功能，只在授权的群组中提供服务，未授权群组会自动退出。

### 工作原理

1. **新群组检测**：Bot 被添加到群组时自动检查白名单状态
2. **白名单验证**：每条消息都会验证群组是否在白名单中
3. **自动退出**：未授权群组会收到提示消息后自动退出
4. **管理员控制**：只有超级管理员可以管理白名单

### 白名单管理

#### 获取群组 ID
在群组中发送任意消息，然后查看日志可以看到 chat_id：
```bash
make dev-logs  # 查看日志中的 chat_id
```

或者使用 [@getidsbot](https://t.me/getidsbot) 等工具获取。

#### 添加群组到白名单
```bash
/whitelist add -1001234567890 测试群组
```

#### 移除群组
```bash
/whitelist remove -1001234567890
```

#### 查看白名单
```bash
/whitelist
```

### 注意事项

- ⚠️ Bot 启动后默认不在任何群组的白名单中
- ⚠️ 请在添加到正式群组前先将其加入白名单
- ⚠️ 私聊不受白名单限制

## 📁 项目结构

```
tg-guard-bot/
├── src/
│   ├── bot/                    # Telegram 交互层
│   │   ├── handlers/           # 命令/事件处理
│   │   │   ├── verification.py # 入群验证
│   │   │   ├── moderation.py   # 群管理命令
│   │   │   ├── antispam.py     # 反垃圾处理
│   │   │   ├── cleanup.py      # 用户清理命令
│   │   │   └── admin.py        # 管理员命令
│   │   ├── middlewares/        # 中间件
│   │   └── filters/            # 自定义过滤器
│   ├── services/               # 业务逻辑层
│   │   ├── verification.py     # 验证服务
│   │   ├── moderation.py       # 群管理服务
│   │   ├── spam_detector.py    # 反垃圾服务
│   │   ├── cleanup.py          # 清理服务
│   │   └── member_query.py     # 成员查询服务（Telethon）
│   ├── ml/                     # AI/ML 模块
│   │   ├── rule_engine.py      # 规则引擎
│   │   ├── classifier.py       # ML 分类器
│   │   ├── embedder.py         # 语义嵌入
│   │   └── ai_detector.py      # AI 检测器（文本 + Vision）
│   ├── models/                 # 数据模型
│   │   ├── group.py            # 群组配置
│   │   ├── user.py             # 用户/警告
│   │   ├── spam_sample.py      # 垃圾样本
│   │   ├── report.py           # 举报记录
│   │   └── audit_log.py        # 操作日志
│   ├── repositories/           # 数据访问层
│   │   ├── group_repo.py
│   │   ├── user_repo.py
│   │   ├── spam_repo.py
│   │   └── report_repo.py
│   └── core/                   # 核心配置
│       ├── config.py           # 配置管理
│       ├── database.py         # DB 连接
│       ├── redis.py            # Redis 连接
│       ├── telethon_client.py  # Telethon 客户端（大群管理）
│       └── health.py           # 健康检查
├── captcha-webapp/             # 统一 CAPTCHA WebApp
│   ├── index.html              # 前端页面
│   ├── functions/api/          # Cloudflare Functions
│   │   ├── config.js           # 配置 API
│   │   └── verify.js           # 验证 API
│   ├── wrangler.toml           # Cloudflare 配置
│   └── README.md               # 部署指南
├── altcha-backend/             # ALTCHA PHP 后端
│   ├── challenge.php           # 生成挑战
│   ├── verify.php              # 验证解答
│   ├── config.php.example      # 配置模板
│   ├── composer.json           # Composer 依赖
│   └── README.md               # Serv00 部署指南
├── scripts/                    # 工具脚本
│   ├── migrate.py              # 数据库迁移
│   ├── backup.py               # 数据库备份
│   └── train_model.py          # 模型训练
├── data/models/                # 预训练模型
├── logs/                       # 日志文件
├── backups/                    # 数据库备份
├── docker-compose.yml          # Docker 编排
├── docker-compose.prod.yml     # 生产环境配置
├── Dockerfile                  # Docker 镜像
├── Makefile                    # 命令快捷方式
└── pyproject.toml              # 项目依赖
```

## ⚙️ 配置说明

### 环境变量

#### 基础配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `BOT_TOKEN` | Telegram Bot Token | - | ✅ |
| `ADMIN_IDS` | 超级管理员 ID（逗号分隔） | - | ✅ |
| `DB_PASSWORD` | 数据库密码 | postgres | ❌ |
| `REDIS_PASSWORD` | Redis 密码 | 空 | ❌ |
| `MODEL_SIGNATURE_KEY` | 模型文件签名密钥（至少 32 字符） | - | ✅ |
| `LOG_LEVEL` | 日志级别 | INFO | ❌ |

#### Telethon 配置（用于大群管理）

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `TELETHON_API_ID` | Telegram API ID（从 my.telegram.org 获取） | - | ✅ (使用 /cleanup 时) |
| `TELETHON_API_HASH` | Telegram API Hash | - | ✅ (使用 /cleanup 时) |
| `TELETHON_SESSION_PATH` | Session 文件路径 | ./data/user_bot.session | ❌ |
| `TELETHON_ENABLED` | 是否启用 Telethon 客户端 | false | ❌ |

**注意**：首次启动时会提示登录手机号并输入验证码，生成 session 文件后后续无需再次登录。

#### 活跃度系统配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `ACTIVITY_MAX_CONFIDENCE_REDUCTION` | 活跃度最大置信度减少值 | 0.15 | ❌ |
| `ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD` | 活跃度跳过垃圾检测阈值（0=使用群组配置） | 0 | ❌ |

> 说明：群组可通过 `/activity` 命令控制是否限制非文本消息，活跃度记录、置信度修正、检测豁免功能始终工作。

#### 反垃圾配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `CAS_ENABLED` | 启用 CAS 黑名单检查（入群 + 消息阶段） | false | ❌ |
| `CAS_API_URL` | CAS API 基础 URL | https://api.cas.chat | ❌ |
| `CAS_CHECK_TIMEOUT` | CAS 检查超时（秒） | 5 | ❌ |
| `CAS_CACHE_TTL` | CAS 缓存 TTL（秒） | 86400 | ❌ |
| `SPAM_THRESHOLD_RULE` | 规则引擎阈值 | 0.8 | ❌ |
| `SPAM_THRESHOLD_ML` | ML 分类器阈值 | 0.7 | ❌ |
| `SPAM_THRESHOLD_EMBEDDING` | Embedding 阈值 | 0.75 | ❌ |
| `SPAM_MIN_TEXT_LENGTH` | 最小标准化文本长度（低于此长度跳过检测，1个汉字/全角字符=1标准长度，2个英文字符=1标准长度） | 10 | ❌ |
| `REGEX_RULES_ENABLED` | 启用高级正则规则引擎 | true | ❌ |
| `REGEX_RULES_CONFIG_PATH` | 自定义规则配置文件路径 | config/spam_rules.json | ❌ |
| `REGEX_RULES_MAX_TEXT_LENGTH` | 正则规则检测的最大文本长度 | 500 | ❌ |

> 说明：CAS 检查采用”失败放行”降级策略；Redis 不可用时会直连 API（仍失败放行），避免误伤正常用户。

#### CAPTCHA 验证配置（可选）

所有外部 CAPTCHA 服务都需要先部署统一 WebApp 到 Cloudflare Pages。

| 变量 | 说明 | 必填 |
|------|------|------|
| `CAPTCHA_WEBAPP_URL` | 统一 CAPTCHA WebApp 地址 | ✅ (使用外部 CAPTCHA 时) |
| `CAPTCHA_SIGNATURE_KEY` | 签名密钥（至少 32 字符） | ✅ (使用外部 CAPTCHA 时) |
| `MODEL_SIGNATURE_KEY` | 模型文件签名密钥（至少 32 字符） | ✅ |

**Friendly Captcha**（隐私友好，支持多 key 轮换）：
```env
FRIENDLY_ENABLED=true
FRIENDLY_KEYS='[{"sitekey":"FCMAV...","apikey":"fc-sk-..."}]'
```

**hCaptcha**（图片验证）：
```env
HCAPTCHA_ENABLED=true
HCAPTCHA_SITE_KEY=your_site_key
HCAPTCHA_SECRET_KEY=your_secret_key
```

**MTCaptcha**（自适应验证）：
```env
MTCAPTCHA_ENABLED=true
MTCAPTCHA_SITE_KEY=MTPublic-...
MTCAPTCHA_PRIVATE_KEY=MTPrivate-...
```

**ALTCHA**（开源 PoW，需部署 PHP 后端到 Serv00）：
```env
ALTCHA_ENABLED=true
ALTCHA_API_URL=https://xxx.serv00.net/altcha
ALTCHA_HMAC_KEY=<64字符hex密钥>
```

**Turnstile**（Cloudflare 无感验证，已集成到统一 WebApp）：
```env
TURNSTILE_ENABLED=true
# Turnstile Site Key（可选）：用于随机验证可用性判断；留空则随机验证不会选中 Turnstile
# （该值为公开信息，可与 WebApp 端的 TURNSTILE_SITE_KEY 保持一致）
TURNSTILE_SITE_KEY=
# ✅ 推荐：使用统一 CAPTCHA WebApp（与其他服务共用）
# 配置 CAPTCHA_WEBAPP_URL 后会自动使用统一 WebApp
```

> ✅ 说明：当前“随机验证”对 Turnstile 的可用性判断依赖 `TURNSTILE_SITE_KEY`（见 `src/services/verification.py:1051`）。
> - **TURNSTILE_SECRET_KEY 不在 Bot 端配置**：它应配置在统一 WebApp（Cloudflare Pages/Functions）的环境变量 `TURNSTILE_SECRET_KEY` 中（见 `captcha-webapp/README.md`、`captcha-webapp/functions/api/verify.js`）。
> - 如果你希望 Turnstile 可能被随机选中，请在 Bot 端同时配置 Turnstile Site Key；直接指定 Turnstile 验证不受影响。

#### AI Vision 多模态检测配置（可选）

用于检测图片/贴纸垃圾内容，支持主备双服务商自动回退。

**最简配置**（复用文本 key/base）：
```env
# 文本检测：便宜纯文本模型
AI_SPAM_ENABLED=true
AI_SPAM_API_KEY=sk-xxx
AI_SPAM_API_BASE=https://api.openai.com/v1
AI_SPAM_MODEL=deepseek-chat

# 图片检测：多模态模型（key/base 留空自动回退上面的配置）
AI_SPAM_VISION_ENABLED=true
AI_SPAM_VISION_MODEL=gpt-4o-mini
```

**完整配置**（主备双服务商）：
```env
# Vision 主服务商
AI_SPAM_VISION_ENABLED=true
AI_SPAM_VISION_API_KEY=sk-vision-main-xxx  # 留空回退 AI_SPAM_API_KEY
AI_SPAM_VISION_API_BASE=https://api.openai.com/v1  # 留空回退 AI_SPAM_API_BASE
AI_SPAM_VISION_MODEL=gpt-4o-mini
AI_SPAM_VISION_DETAIL=low  # OpenAI image_url.detail: low/high/auto
AI_SPAM_VISION_TIMEOUT=30

# Vision 备服务商
AI_SPAM_VISION_BACKUP_ENABLED=true
AI_SPAM_VISION_BACKUP_API_KEY=  # 留空回退 AI_SPAM_BACKUP_API_KEY
AI_SPAM_VISION_BACKUP_API_BASE=  # 留空回退 AI_SPAM_BACKUP_API_BASE
AI_SPAM_VISION_BACKUP_MODEL=claude-3-5-sonnet
AI_SPAM_VISION_BACKUP_DETAIL=low
AI_SPAM_VISION_BACKUP_TIMEOUT=30
```

> **成本优化提示**：文本消息占比 >95%，将文本检测配置为便宜模型（如 deepseek-chat），图片检测使用多模态模型，可节省 90%+ API 成本。

📚 详细配置参考：
- [.env.example](.env.example) - 完整配置模板
- [captcha-webapp/README.md](captcha-webapp/README.md) - WebApp 部署指南
- [altcha-backend/README.md](altcha-backend/README.md) - ALTCHA 后端部署指南

## 🔒 安全建议

1. **修改默认密码**：生产环境必须修改 `DB_PASSWORD` 和 `REDIS_PASSWORD`
2. **限制端口暴露**：生产环境注释掉 `docker-compose.yml` 中的端口映射（或在防火墙层面限制访问）
3. **启用防火墙**：使用 UFW 限制只允许必要的端口
4. **使用 SSH 密钥**：禁用 SSH 密码登录
5. **定期备份**：使用 `make backup`（PostgreSQL + Redis）或配置自动备份
6. **监控日志**：定期检查 `logs/error_*.log`

## 📈 性能指标

| 指标 | 基础版 | 启用 Vision |
|------|--------|-------------|
| 镜像大小 | ~300MB | ~300MB |
| 内存占用（空闲） | ~200MB | ~200MB |
| 内存占用（峰值） | ~500MB | ~800MB |
| 文本消息处理 | <100ms | <100ms |
| 图片消息处理（Vision） | N/A | 1-3s |

> **说明**：v1.5.0 删除 OCR 功能后，镜像和内存占用大幅降低。Vision 多模态检测采用 API 调用，无本地模型加载。

## 🗺️ 开发路线图

- [x] **Phase 1**: 基础框架（Docker + PostgreSQL + Redis）
- [x] **Phase 2**: 入群验证（7 种验证方式 + 私聊验证）
- [x] **Phase 3**: 群管理（Kick/Mute/Ban/Warn）
- [x] **Phase 4**: 反垃圾系统（三阶段检测管道）
- [x] **Phase 5**: AI Vision 多模态检测（图片/贴纸垃圾检测）
- [x] **Phase 6**: 部署优化（监控/备份/文档）
- [x] **Phase 7**: 验证系统增强（7 种验证 + 动态超时配置）
- [x] **Phase 8**: 多 CAPTCHA 集成（Friendly/hCaptcha/MTCaptcha/ALTCHA + 统一 WebApp）
- [x] **v1.1.0**: 上下文一致性检测（降低误判率）
- [x] **v1.2.0**: 高级正则规则引擎 + @username 解析
- [x] **v1.5.0**: 删除 OCR，重构 Vision 为独立配置（主备双服务商）

## 🤝 贡献

欢迎贡献代码！请遵循以下步骤：

1. Fork 本项目
2. 创建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 提交 Pull Request

## 📄 许可证

本项目基于 MIT 许可证开源 - 详见 [LICENSE](LICENSE) 文件

## 🙏 致谢

### 核心框架
- [aiogram](https://github.com/aiogram/aiogram) - 优秀的 Telegram Bot 框架
- [Telethon](https://github.com/LonamiWebs/Telethon) - 强大的 Telegram Client API
- [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) - Python SQL 工具包和 ORM
- [Redis](https://redis.io/) - 高性能内存数据库
- [PostgreSQL](https://www.postgresql.org/) - 强大的开源关系型数据库

### 机器学习与 AI
- [scikit-learn](https://github.com/scikit-learn/scikit-learn) - 机器学习库
- [jieba](https://github.com/fxsjy/jieba) - 中文分词工具
- [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) - 中文语义嵌入模型
- [fastembed](https://github.com/qdrant/fastembed) - 快速文本嵌入库

### 开发工具
- [Pydantic](https://github.com/pydantic/pydantic) - 数据验证库
- [Loguru](https://github.com/Delgan/loguru) - 优雅的日志库
- [Ruff](https://github.com/astral-sh/ruff) - 快速 Python Linter
- [Docker](https://www.docker.com/) - 容器化平台

### 特别感谢
- 所有贡献者和使用者
- 开源社区的支持和帮助

## 📞 联系方式

- 提交 Issue: [GitHub Issues](https://github.com/cnsunyour/tg-guard-bot/issues)
- 技术交流: [Telegram 群组](https://t.me/tg_smart_guard)

---

**⭐ 如果这个项目对你有帮助，请给个 Star！**
