# 生产环境部署指南

本文档详细说明如何将 Telegram Guard Bot 部署到生产环境。

---

## 📋 准备工作

### 1. 服务器要求

| 项目 | 最低配置 | 推荐配置 |
|------|---------|---------|
| CPU | 1 vCPU | 2 vCPU |
| 内存 | 2GB | 4GB（启用 OCR） |
| 存储 | 20GB SSD | 40GB SSD |
| 带宽 | 1TB/月 | 无限 |
| 系统 | Debian 11+ / Ubuntu 20.04+ | Debian 12 / Ubuntu 22.04 |

**推荐 VPS 供应商**:
- Hetzner Cloud (€4.5/月 起)
- DigitalOcean ($6/月 起)
- Vultr ($6/月 起)
- Linode ($5/月 起)

### 2. 必备软件

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER

# 安装 Docker Compose
sudo apt install -y docker-compose

# 安装 Make（可选，简化命令）
sudo apt install -y make

# 安装 Git
sudo apt install -y git
```

### 3. 获取 Bot Token

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 创建新 Bot
3. 按提示设置 Bot 名称和用户名
4. 保存生成的 Token（格式：`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`）

---

## 🚀 部署步骤

### 步骤 1: 克隆代码

```bash
# 克隆仓库
git clone https://github.com/cnsunyour/tg-guard-bot.git
cd tg-guard-bot

# 或者上传代码到服务器
scp -r ./tg-guard-bot user@your-server:/path/to/
```

### 步骤 2: 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑环境变量
nano .env
```

**必须配置的变量**:

```bash
# Telegram Bot 配置
BOT_TOKEN=your_bot_token_here  # ⚠️ 必填
ADMIN_IDS=123456789            # ⚠️ 必填：你的 Telegram User ID

# 数据库配置（生产环境请修改密码）
DB_PASSWORD=your_secure_password_here  # ⚠️ 强烈建议修改

# Redis 配置（生产环境请设置密码）
REDIS_PASSWORD=your_redis_password     # ⚠️ 强烈建议设置

# 是否启用 OCR（需要 4GB RAM）
ENABLE_OCR=false  # 根据需要设置为 true
```

**获取你的 Telegram User ID**:
1. 在 Telegram 中找到 [@userinfobot](https://t.me/userinfobot)
2. 发送任意消息
3. 复制返回的 `Id` 数字

### 步骤 3: 构建和启动

```bash
# 方式 1: 使用 Makefile（推荐）
make prod-build       # 构建镜像（不启用 OCR）
# 或
make prod-build-ocr   # 构建镜像（启用 OCR）

make prod-up          # 启动服务

# 方式 2: 使用 Docker Compose
docker-compose -f docker-compose.yml -f docker-compose.prod.yml build
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### 步骤 4: 初始化数据库

```bash
# 运行数据库迁移
make db-migrate
# 或
docker-compose exec bot python scripts/migrate.py
```

### 步骤 5: 验证部署

```bash
# 查看服务状态
make status
# 或
docker-compose ps

# 查看日志
make prod-logs
# 或
docker-compose logs -f bot

# 健康检查（在 Telegram 中发送给 Bot）
/health
```

**预期输出**:
```
✅ 系统健康状态

⏱️ 运行时间: 0天 0小时 1分钟 23秒
🔄 检查次数: 1

✅ 数据库: 5.23ms
✅ Redis: 2.15ms

💻 系统资源
• CPU: 5.2% (2 核)
• 内存: 450/4096 MB (11.0%)
• 磁盘: 3.5/40.0 GB (8.8%)
```

---

## 🔧 初始化配置

### 1. 添加 Bot 到群组

1. 创建一个测试群组
2. 将 Bot 添加为管理员
3. 授予以下权限：
   - ✅ 删除消息
   - ✅ 封禁用户
   - ✅ 邀请用户
   - ✅ 管理视频聊天（可选）

### 2. 配置群组

```bash
# 在群组中发送（需要管理员权限）
/setverify         # 设置验证方式
/antispam          # 启用反垃圾功能
```

### 3. 训练反垃圾模型

```bash
# 添加示例训练数据
make train-samples

# 训练模型
make train-model

# 或者使用 Docker Compose
docker-compose exec bot python scripts/train_model.py --add-samples
docker-compose exec bot python scripts/train_model.py --train
```

---

## 🛡️ 安全加固

### 1. 防火墙配置

```bash
# 安装 UFW
sudo apt install -y ufw

# 允许 SSH（⚠️ 先允许 SSH 避免被锁定）
sudo ufw allow 22/tcp

# 允许 HTTP/HTTPS（如果需要 Nginx）
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# 启用防火墙
sudo ufw enable

# 查看状态
sudo ufw status
```

### 2. 限制 Docker 端口暴露

默认配置中，PostgreSQL 和 Redis 端口仅在本地暴露。**生产环境建议移除端口映射**：

编辑 `docker-compose.yml`，注释掉以下行：

```yaml
postgres:
  # ports:
  #   - "5432:5432"  # 注释掉，仅容器内访问

redis:
  # ports:
  #   - "6379:6379"  # 注释掉，仅容器内访问
```

### 3. 设置 SSH 密钥登录

```bash
# 本地生成密钥对（如果还没有）
ssh-keygen -t ed25519 -C "your_email@example.com"

# 上传公钥到服务器
ssh-copy-id user@your-server

# 禁用密码登录（可选，但推荐）
sudo nano /etc/ssh/sshd_config
# 修改: PasswordAuthentication no
sudo systemctl restart sshd
```

---

## 📊 监控和维护

### 1. 查看日志

```bash
# 实时日志
make prod-logs

# 查看最近 100 行
docker-compose logs --tail=100 bot

# 查看错误日志
docker-compose exec bot cat logs/error_$(date +%Y-%m-%d).log
```

### 2. 数据库备份

```bash
# 手动备份
make db-backup

# 或使用脚本
docker-compose exec bot python scripts/backup.py --backup

# 查看备份列表
docker-compose exec bot python scripts/backup.py --list

# 恢复备份
docker-compose exec bot python scripts/backup.py --restore backup_20240115_120000.sql
```

**设置自动备份 (Cron)**:

```bash
# 编辑 crontab
crontab -e

# 添加每天凌晨 3 点备份
0 3 * * * cd /path/to/tg-guard-bot && make db-backup >> /var/log/tg-guard-backup.log 2>&1
```

### 3. 性能监控

```bash
# 查看资源使用
make status

# 在 Telegram 中查看统计
/stats  # 发送给 Bot（仅超级管理员）
```

### 4. 日志轮转

日志已配置自动轮转：
- 普通日志：每天轮转，保留 7 天，压缩为 `.zip`
- 错误日志：每天轮转，保留 30 天，压缩为 `.zip`

查看日志文件：

```bash
ls -lh logs/
```

---

## 🔄 更新部署

### 拉取最新代码

```bash
cd /path/to/tg-guard-bot

# 拉取代码
git pull origin main

# 重新构建并启动
make prod-down
make prod-build
make prod-up

# 查看日志确认启动成功
make prod-logs
```

### 仅重启 Bot（不重新构建）

```bash
make prod-restart
```

---

## 🚨 故障排查

### 问题 1: Bot 无响应

**症状**: 发送命令给 Bot 没有回复

**排查步骤**:

```bash
# 1. 检查容器状态
docker-compose ps

# 2. 查看 Bot 日志
docker-compose logs --tail=50 bot

# 3. 检查网络连接
docker-compose exec bot ping -c 3 api.telegram.org

# 4. 重启 Bot
make prod-restart
```

### 问题 2: 数据库连接失败

**症状**: 日志显示 "database connection failed"

**排查步骤**:

```bash
# 1. 检查 PostgreSQL 状态
docker-compose exec postgres pg_isready -U postgres

# 2. 检查环境变量
docker-compose exec bot env | grep DB_

# 3. 手动连接测试
docker-compose exec postgres psql -U postgres -d tg_guard -c "SELECT 1"

# 4. 重启数据库
docker-compose restart postgres
```

### 问题 3: 内存不足

**症状**: 容器频繁重启，系统日志显示 OOM

**解决方法**:

```bash
# 1. 检查内存使用
free -h
docker stats

# 2. 禁用 OCR（如果启用了）
# 编辑 .env: ENABLE_OCR=false
make prod-down
make prod-build
make prod-up

# 3. 升级 VPS 到 4GB RAM
```

### 问题 4: OCR 初始化失败

**症状**: 日志显示 "PaddleOCR 未安装"

**解决方法**:

```bash
# 确认 ENABLE_OCR 设置
cat .env | grep ENABLE_OCR

# 重新构建（确保传递 build-arg）
make prod-build-ocr

# 或
docker-compose -f docker-compose.yml -f docker-compose.prod.yml build --build-arg ENABLE_OCR=true
```

---

## 📈 性能优化

### 1. 数据库优化

```bash
# 进入数据库
make db-shell

# 创建索引（如果 migrate.py 未自动创建）
CREATE INDEX IF NOT EXISTS idx_warnings_group_user ON warnings(group_id, user_id);
CREATE INDEX IF NOT EXISTS idx_spam_samples_is_spam ON spam_samples(is_spam);

# 分析表统计信息
ANALYZE warnings;
ANALYZE spam_samples;
ANALYZE audit_logs;
```

### 2. Redis 优化

Redis 配置已在 `docker-compose.prod.yml` 中优化：
- 最大内存: 256MB
- 淘汰策略: `allkeys-lru`
- 持久化: RDB + AOF

### 3. 调整工作线程数（可选）

编辑 `src/main.py`，在 `start_polling` 中添加：

```python
await dp.start_polling(
    bot,
    allowed_updates=dp.resolve_used_update_types(),
    polling_timeout=30,  # 轮询超时
)
```

---

## 📝 维护检查清单

### 每日检查
- [ ] 查看错误日志: `cat logs/error_*.log`
- [ ] 检查容器状态: `make status`
- [ ] 验证 Bot 响应: 发送 `/help` 测试

### 每周检查
- [ ] 数据库备份验证: `make db-backup`
- [ ] 查看性能指标: `/health` 和 `/stats`
- [ ] 检查磁盘空间: `df -h`
- [ ] 清理旧日志: `ls -lh logs/`

### 每月检查
- [ ] 系统更新: `sudo apt update && sudo apt upgrade`
- [ ] Docker 镜像更新: `docker-compose pull`
- [ ] 重新训练反垃圾模型: `make train-model`
- [ ] 检查安全公告

---

## 🔗 相关文档

- [README.md](README.md) - 项目概览
- [QUICKSTART.md](QUICKSTART.md) - 快速开始指南
- [PHASE3_TESTING.md](PHASE3_TESTING.md) - 群管理测试
- [PHASE5_OCR_TESTING.md](PHASE5_OCR_TESTING.md) - OCR 测试

---

## 💡 最佳实践

1. **定期备份**: 每天至少备份一次数据库
2. **监控日志**: 定期查看错误日志，及时发现问题
3. **性能监控**: 使用 `/health` 命令定期检查系统状态
4. **安全更新**: 及时更新系统和 Docker 镜像
5. **测试环境**: 有条件的话，先在测试环境验证更新
6. **文档记录**: 记录所有配置更改和问题解决过程
7. **容量规划**: 根据群组数量和消息量，及时升级资源

---

## 🆘 获取帮助

遇到问题？

1. 查看[故障排查](#-故障排查)章节
2. 搜索 [GitHub Issues](https://github.com/cnsunyour/tg-guard-bot/issues)
3. 提交新的 Issue（包含日志和错误信息）
4. 加入技术交流群（如果有）

---

**部署成功！** 🎉

记得定期维护和备份，祝你使用愉快！
