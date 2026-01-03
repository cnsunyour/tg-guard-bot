# 🚀 快速开始安全指南

> **重要提示**：在部署到生产环境前，请务必阅读完整的 [SECURITY.md](SECURITY.md) 和 [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)

---

## ⚡ 5 分钟快速安全配置

### 1. 生成强密码

```bash
# 数据库密码
export DB_PASSWORD=$(openssl rand -base64 32)

# Redis 密码
export REDIS_PASSWORD=$(openssl rand -base64 32)

# 模型签名密钥
export MODEL_SIGNATURE_KEY=$(openssl rand -hex 32)

# 保存到 .env 文件
cat > .env << EOF
# Telegram Bot 配置
BOT_TOKEN=<YOUR_BOT_TOKEN_FROM_BOTFATHER>
ADMIN_IDS=<YOUR_TELEGRAM_USER_ID>

# 数据库配置
DB_HOST=postgres
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=${DB_PASSWORD}
DB_NAME=tg_guard

# Redis 配置
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=${REDIS_PASSWORD}

# 应用配置
LOG_LEVEL=INFO
DEBUG=false

# 反垃圾配置
SPAM_THRESHOLD_RULE=0.8
SPAM_THRESHOLD_ML=0.7
SPAM_THRESHOLD_EMBEDDING=0.75

# OCR 配置
ENABLE_OCR=false

# 验证配置
VERIFICATION_TIMEOUT=60
MAX_WARNINGS=3

# AI 模型路径
ML_MODEL_PATH=data/models/spam_classifier.pkl
EMBEDDING_MODEL_NAME=BAAI/bge-small-zh-v1.5

# 模型安全配置
MODEL_SIGNATURE_KEY=${MODEL_SIGNATURE_KEY}
EOF

# 设置安全权限
chmod 600 .env
```

### 2. 验证配置

```bash
# 检查 .env 文件
grep -v '^#' .env | grep -v '^$'

# 确保没有示例值
grep -E "your_.*_here|123456789|CHANGE_ME" .env && echo "⚠️ 发现示例值，请修改！" || echo "✅ 配置检查通过"
```

### 3. 设置文件权限

```bash
# 创建数据目录
mkdir -p data/models logs

# 设置权限（容器用户 UID 1000）
sudo chown -R 1000:1000 data/ logs/
chmod 755 data/models logs
```

### 4. 安全部署

```bash
# 构建镜像
docker-compose build

# 启动数据库
docker-compose up -d postgres redis

# 初始化数据库
sleep 10  # 等待数据库启动
docker-compose run --rm bot python scripts/migrate.py init

# 启动 Bot
docker-compose up -d bot

# 查看日志
docker-compose logs -f bot
```

### 5. 验证安全配置

```bash
# 1. 检查容器用户（应该是 appuser）
docker-compose exec bot whoami

# 2. 检查端口暴露（数据库端口不应该出现）
sudo netstat -tlnp | grep -E "(5432|6379)"

# 3. 检查服务状态
docker-compose ps

# 4. 测试 Bot 功能
# 在 Telegram 中发送 /start 命令
```

---

## 🔒 安全配置检查清单（生产环境）

### 必须完成（P0）

- [ ] ✅ 所有密码使用强密码（≥16字符）
- [ ] ✅ BOT_TOKEN 是真实的，不是示例值
- [ ] ✅ ADMIN_IDS 设置正确
- [ ] ✅ DEBUG=false
- [ ] ✅ LOG_LEVEL=INFO
- [ ] ✅ 数据库端口未对外暴露
- [ ] ✅ Redis 端口未对外暴露
- [ ] ✅ 容器以非 root 用户运行
- [ ] ✅ .env 文件权限为 600

### 强烈推荐（P1）

- [ ] 🔥 配置防火墙（UFW/iptables）
- [ ] 🔥 启用自动安全更新
- [ ] 🔥 配置日志监控
- [ ] 🔥 设置数据库自动备份
- [ ] 🔥 配置 SSL/TLS（如有 webhook）

### 建议配置（P2）

- [ ] 📋 配置反向代理（Nginx）
- [ ] 📋 启用 fail2ban
- [ ] 📋 配置资源限制
- [ ] 📋 设置监控告警
- [ ] 📋 定期安全扫描

---

## 🛡️ 安全功能说明

### 已启用的安全保护

| 功能 | 说明 | 配置 |
|------|------|------|
| **速率限制** | 防止 DoS 攻击 | 消息: 3/秒, 回调: 5/秒 |
| **权限验证** | 所有管理命令需要权限 | 自动检查 |
| **日志脱敏** | 敏感信息自动脱敏 | 自动启用 |
| **HTML 转义** | 防止 XSS 攻击 | 自动启用 |
| **模型签名** | 防止恶意模型注入 | HMAC-SHA256 |
| **强随机数** | 使用 secrets 模块 | 自动启用 |
| **输入验证** | ID/路径/参数验证 | 自动启用 |

### 可选功能

| 功能 | 说明 | 如何启用 |
|------|------|----------|
| **OCR 检测** | 图片垃圾检测 | `ENABLE_OCR=true` |
| **反垃圾** | 三阶段检测管道 | 默认启用，可通过 `/antispam` 配置 |
| **入群验证** | 防止机器人进群 | 默认启用，可通过 `/setverify` 配置 |

---

## 🚨 常见安全问题

### 问题 1: 数据库密码太弱

```bash
# ❌ 错误
DB_PASSWORD=123456

# ✅ 正确
DB_PASSWORD=$(openssl rand -base64 32)
```

### 问题 2: 使用示例配置

```bash
# ❌ 错误
BOT_TOKEN=your_bot_token_here

# ✅ 正确
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
```

### 问题 3: 数据库端口暴露

```yaml
# ❌ 错误
postgres:
  ports:
    - "5432:5432"  # 对外暴露

# ✅ 正确
postgres:
  # 不配置 ports，仅 Docker 网络内访问
```

### 问题 4: 容器以 root 运行

```yaml
# ❌ 错误
services:
  bot:
    # 没有 user 配置

# ✅ 正确
services:
  bot:
    user: "1000:1000"
```

### 问题 5: DEBUG 模式在生产环境

```bash
# ❌ 错误
DEBUG=true
LOG_LEVEL=DEBUG

# ✅ 正确
DEBUG=false
LOG_LEVEL=INFO
```

---

## 📚 完整文档

- **[SECURITY.md](SECURITY.md)** - 完整安全策略和报告流程
- **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)** - 详细部署检查清单
- **[.env.example](.env.example)** - 环境变量配置示例

---

## 🆘 获取帮助

- **安全问题**：通过 GitHub Security Advisories 私密报告
- **一般问题**：通过 GitHub Issues
- **紧急漏洞**：直接联系维护者

---

## 🔄 定期维护

```bash
# 每周：检查更新
docker-compose pull

# 每月：安全扫描
safety check
bandit -r src/

# 每季度：全面审计
# 运行完整的安全测试套件
```

---

**最后更新**：2025-01-03
**文档版本**：1.0
