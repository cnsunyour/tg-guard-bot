<p align="center">
  <img src="assets/logo/banner-horizontal.svg" alt="Telegram Guard Bot" width="800"/>
</p>

# Telegram Guard Bot

一个功能强大的 Telegram 群管理机器人，支持入群验证、群管理和智能反垃圾功能。

[![Version](https://img.shields.io/badge/version-1.0.1-blue.svg)](https://github.com/cnsunyour/tg-guard-bot/releases/tag/v1.0.1)
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

### 🛡️ 智能反垃圾（三阶段检测）
- **Stage 1: 规则引擎** - 快速过滤关键词、链接、联系方式（~70% 垃圾）
- **Stage 2: ML 分类器** - TF-IDF + SVM 捕获变体（~90% 垃圾）
- **Stage 3: 语义分析** - bge-small-zh-v1.5 Embedding（~98% 垃圾）
- **编辑消息检测** - 应对先发普通消息后编辑成垃圾的手段（支持文本和图片标题）
- **图片 OCR** - EasyOCR 检测图片广告（可选，需 4GB RAM）
- **活跃度系统** - 动态信任机制：
  - 文本消息 +1 活跃度
  - 非文本消息（图片/贴纸/转发/链接）-2 活跃度
  - 高活跃度用户可跳过垃圾检测
  - 低活跃度用户无法发送非文本消息
- **管理员反馈** - 误判纠正，持续优化

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
| aiogram | 3.x | Telegram Bot 框架 |
| Telethon | 1.36+ | Telegram Client API（用于大群成员管理） |
| PostgreSQL | 16 | 主数据库 |
| Redis | 7 | 缓存和队列 |
| SQLAlchemy | 2.0 | ORM |
| scikit-learn | 1.4+ | ML 分类器 |
| fastembed | 0.3+ | 语义嵌入 |
| EasyOCR | 1.7+ | 图片 OCR（可选） |

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
DB_PASSWORD=your_secure_password_here
```

### 3. 启动服务

使用 Make（推荐）：
```bash
make dev-up        # 启动开发环境
make dev-logs      # 查看日志
```

或使用 Docker Compose：
```bash
docker-compose up -d
docker-compose logs -f bot
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
| [PHASE3_TESTING.md](PHASE3_TESTING.md) | 群管理功能测试 |
| [PHASE5_OCR_TESTING.md](PHASE5_OCR_TESTING.md) | OCR 功能测试 |

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
make prod-build-ocr  # 构建生产镜像（启用 OCR）
make prod-up         # 启动生产环境
make prod-down       # 停止生产环境
make prod-restart    # 重启 Bot
make prod-logs       # 查看日志
```

### 数据库
```bash
make db-migrate      # 运行数据库迁移
make db-backup       # 备份数据库
make db-restore      # 恢复数据库
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
- `/kick @user` - 踢出成员
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
- `/notspam` - 标记非垃圾（误判反馈，帮助优化模型）
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
│   │   └── ocr.py              # 图片 OCR
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
| `LOG_LEVEL` | 日志级别 | INFO | ❌ |

#### Telethon 配置（用于大群管理）

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `TELETHON_API_ID` | Telegram API ID（从 my.telegram.org 获取） | - | ✅ (使用 /cleanup 时) |
| `TELETHON_API_HASH` | Telegram API Hash | - | ✅ (使用 /cleanup 时) |
| `TELETHON_SESSION_NAME` | Session 文件名 | bot_session | ❌ |

**注意**：首次启动时会提示登录手机号并输入验证码，生成 session 文件后后续无需再次登录。

#### 活跃度系统配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `ACTIVITY_ENABLED` | 全局启用活跃度系统 | true | ❌ |
| `ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD` | 活跃度跳过垃圾检测阈值（0=禁用） | 0 | ❌ |

#### 反垃圾配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `ENABLE_OCR` | 启用 OCR 功能 | false | ❌ |
| `SPAM_THRESHOLD_ML` | ML 分类器阈值 | 0.7 | ❌ |
| `SPAM_THRESHOLD_EMBEDDING` | Embedding 阈值 | 0.75 | ❌ |

#### CAPTCHA 验证配置（可选）

所有外部 CAPTCHA 服务都需要先部署统一 WebApp 到 Cloudflare Pages。

| 变量 | 说明 | 必填 |
|------|------|------|
| `CAPTCHA_WEBAPP_URL` | 统一 CAPTCHA WebApp 地址 | ✅ (使用外部 CAPTCHA 时) |
| `CAPTCHA_SIGNATURE_KEY` | 签名密钥（64 字符 hex） | ✅ (使用外部 CAPTCHA 时) |

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
# ✅ 推荐：使用统一 CAPTCHA WebApp（与其他服务共用）
# 配置 CAPTCHA_WEBAPP_URL 后会自动使用统一 WebApp

# ⚠️ 已过时：独立 Turnstile WebApp（向后兼容）
# 如果未配置 CAPTCHA_WEBAPP_URL，将回退使用此配置
TURNSTILE_WEBAPP_URL=https://verify.xxx.pages.dev
TURNSTILE_SIGNATURE_KEY=<64字符hex密钥>
```

📚 详细配置参考：
- [.env.example](.env.example) - 完整配置模板
- [captcha-webapp/README.md](captcha-webapp/README.md) - WebApp 部署指南
- [altcha-backend/README.md](altcha-backend/README.md) - ALTCHA 后端部署指南

## 🔒 安全建议

1. **修改默认密码**：生产环境必须修改 `DB_PASSWORD` 和 `REDIS_PASSWORD`
2. **限制端口暴露**：生产环境注释掉 `docker-compose.yml` 中的端口映射
3. **启用防火墙**：使用 UFW 限制只允许必要的端口
4. **使用 SSH 密钥**：禁用 SSH 密码登录
5. **定期备份**：使用 `make db-backup` 或配置自动备份
6. **监控日志**：定期检查 `logs/error_*.log`

## 📈 性能指标

| 指标 | 无 OCR | 有 OCR |
|------|--------|--------|
| 镜像大小 | ~300MB | ~1.2GB |
| 内存占用（空闲） | ~200MB | ~400MB |
| 内存占用（峰值） | ~300MB | ~1.5GB |
| 文本消息处理 | <100ms | <100ms |
| 图片消息处理 | N/A | 1-5s |

## 🗺️ 开发路线图

- [x] **Phase 1**: 基础框架（Docker + PostgreSQL + Redis）
- [x] **Phase 2**: 入群验证（7 种验证方式 + 私聊验证）
- [x] **Phase 3**: 群管理（Kick/Mute/Ban/Warn）
- [x] **Phase 4**: 反垃圾系统（三阶段检测管道）
- [x] **Phase 5**: 图片 OCR（EasyOCR）
- [x] **Phase 6**: 部署优化（监控/备份/文档）
- [x] **Phase 7**: 验证系统增强（7 种验证 + 动态超时配置）
- [x] **Phase 8**: 多 CAPTCHA 集成（Friendly/hCaptcha/MTCaptcha/ALTCHA + 统一 WebApp）

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

- [aiogram](https://github.com/aiogram/aiogram) - 优秀的 Telegram Bot 框架
- [EasyOCR](https://github.com/JaidedAI/EasyOCR) - 强大的 OCR 工具
- [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) - 中文语义嵌入模型

## 📞 联系方式

- 提交 Issue: [GitHub Issues](https://github.com/cnsunyour/tg-guard-bot/issues)
- 技术交流: [Telegram 群组]（可选）

---

**⭐ 如果这个项目对你有帮助，请给个 Star！**
