# 快速开始指南

本指南将帮助你快速部署和测试 Telegram Guard Bot。

## 📋 前提条件

1. 安装 Docker 和 Docker Compose
2. 获取 Telegram Bot Token
3. 获取你的 Telegram User ID

## 🚀 快速部署

### 1. 获取 Bot Token

1. 在 Telegram 中找到 [@BotFather](https://t.me/botfather)
2. 发送 `/newbot` 创建新 bot
3. 按提示设置 bot 名称
4. 复制获得的 token（格式：`1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`）

### 2. 获取你的 User ID

1. 在 Telegram 中找到 [@userinfobot](https://t.me/userinfobot)
2. 发送 `/start`
3. 复制你的 User ID（纯数字）

### 3. 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件
nano .env  # 或使用你喜欢的编辑器
```

修改以下配置：
```env
BOT_TOKEN=你的_bot_token_这里
ADMIN_IDS=你的_user_id_这里
DB_PASSWORD=设置一个强密码
```

### 4. 启动服务

```bash
# 构建并启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f bot
```

### 5. 设置 Bot 权限

1. 将 Bot 添加到你的测试群组
2. 将 Bot 设为管理员，并授予以下权限：
   - ✅ 删除消息
   - ✅ 禁止用户
   - ✅ 邀请用户
   - ✅ 限制成员

## 🧪 测试功能

### 测试入群验证

1. **发送配置命令**
   ```
   /start
   /setverify
   ```

2. **选择验证方式**
   - 🔘 按钮验证（最简单）
   - 🔢 数学验证（中等）
   - 🎯 滑块验证（最难）

3. **测试验证流程**
   - 用另一个账号加入群组
   - 应该会收到验证消息
   - 在 60 秒内完成验证

4. **查看配置**
   ```
   /verifyconfig
   ```

### 验证成功场景

✅ 正确答题/点击 → 恢复权限 → 删除验证消息 → 显示欢迎消息

### 验证失败场景

❌ 答错题目 → 踢出群组
⏰ 超时未答 → 踢出群组

## 📊 查看日志

### 查看实时日志
```bash
docker-compose logs -f bot
```

### 查看数据库
```bash
# 连接到 PostgreSQL
docker-compose exec postgres psql -U postgres -d tg_guard

# 查看群组配置
SELECT * FROM groups;

# 退出
\q
```

### 查看 Redis
```bash
# 连接到 Redis
docker-compose exec redis redis-cli

# 查看所有键
KEYS *

# 查看验证状态
GET verification:群组ID:用户ID

# 退出
exit
```

## 🔧 故障排除

### Bot 无法启动

1. 检查 Token 是否正确
   ```bash
   docker-compose logs bot | grep "token"
   ```

2. 检查数据库连接
   ```bash
   docker-compose ps postgres
   ```

### 验证消息没有发送

1. 确认 Bot 是管理员
2. 检查权限是否完整
3. 查看日志：
   ```bash
   docker-compose logs -f bot
   ```

### 验证超时不工作

1. 检查 Redis 是否运行
   ```bash
   docker-compose ps redis
   ```

2. 检查 Redis 连接
   ```bash
   docker-compose logs bot | grep "redis"
   ```

## 🛑 停止服务

```bash
# 停止服务
docker-compose down

# 停止并删除数据
docker-compose down -v
```

## 📝 常用命令

| 命令 | 说明 |
|------|------|
| `/start` | 查看帮助信息 |
| `/help` | 同 /start |
| `/setverify` | 设置验证方式 |
| `/verifyconfig` | 查看当前配置 |

## 🎯 下一步

现在你已经成功部署并测试了入群验证功能！

接下来可以：
- ✅ Phase 2: 入群验证（已完成）
- ⏳ Phase 3: 群管理功能（计划中）
- ⏳ Phase 4: 反垃圾系统（计划中）
- ⏳ Phase 5: 图片 OCR（计划中）

## 💬 获取帮助

遇到问题？
1. 查看日志：`docker-compose logs -f bot`
2. 检查配置：`cat .env`
3. 重启服务：`docker-compose restart bot`
