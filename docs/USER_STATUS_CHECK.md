# 用户状态检测功能说明

## 📋 功能概述

用户状态检测功能基于 Telethon 客户端，能够检测用户是否为 Telegram 官方标记的异常账号，包括：

- **restricted** - 被 Telegram 限制的用户（违规账号）
- **scam** - 被标记为诈骗的用户
- **fake** - 被标记为虚假账号的用户
- **deleted** - 已删除的账号

当检测到异常用户时，Bot 会自动：
1. 拒绝其加入群组（加入请求或直接加入）
2. 删除其发送的消息
3. 封禁该用户
4. 发送群内通知

---

## 🔧 配置方法

### 1. 启用 Telethon 客户端

用户状态检测功能需要 Telethon 客户端支持。

#### 1.1 获取 API 凭据

访问 https://my.telegram.org/apps 获取：
- `api_id` - 整数 ID
- `api_hash` - 32 位哈希字符串

#### 1.2 生成 Session 文件

运行登录脚本：

```bash
# Docker 环境
docker-compose exec bot python scripts/telethon_login.py

# 本地环境
python scripts/telethon_login.py
```

按提示输入手机号和验证码，成功后会生成 `data/user_bot.session` 文件。

#### 1.3 配置环境变量

在 `.env` 文件中添加：

```bash
# Telethon 配置
TELETHON_ENABLED=true
TELETHON_API_ID=<YOUR_API_ID>
TELETHON_API_HASH=<YOUR_API_HASH>
TELETHON_SESSION_PATH=./data/user_bot.session
```

### 2. 启用用户状态检测

在 `.env` 文件中添加：

```bash
# 用户状态检测配置
USER_STATUS_CHECK_ENABLED=true

# 可选：调整缓存时间（秒），默认 1 小时
USER_STATUS_CACHE_TTL=3600

# 可选：调整重试次数，默认 2 次
USER_STATUS_MAX_RETRIES=2
```

### 3. 重启 Bot

```bash
# Docker 环境
make dev-restart

# 本地环境
# 停止 Bot 后重新运行
python -m src.main
```

---

## 🎯 工作原理

### 检测时机

用户状态检测会在以下场景触发：

1. **用户申请加入群组**（Join Request）
   - 检测顺序：CAS 黑名单 → 用户状态 → 用户信息反垃圾 → 入群验证
   - 如果检测到异常：拒绝加入请求 + 封禁

2. **用户直接加入群组**（通过邀请链接等）
   - 检测顺序：CAS 黑名单 → 用户状态 → 用户信息反垃圾 → 入群验证
   - 如果检测到异常：踢出 + 封禁 + 群内通知

3. **用户发送消息**
   - 中间件检测：CAS 黑名单 → 用户状态
   - 如果检测到异常：删除消息 + 封禁 + 群内通知

### 检测流程

```
用户触发检测
    ↓
检查 Redis 缓存（1 小时 TTL）
    ↓
缓存未命中 → 获取分布式锁
    ↓
调用 Telethon API 获取用户状态
    ↓
检查用户标记：
    - restricted? → 异常用户
    - scam? → 异常用户
    - fake? → 异常用户
    - deleted? → 异常用户
    ↓
写入缓存 + 释放锁
    ↓
返回检测结果
```

### 降级策略

为避免误伤正常用户，采用以下降级策略：

- Telethon 未启用 → 跳过检测（放行）
- API 调用失败 → 放行
- 网络超时 → 放行
- 用户不在群组 → 放行

只有在明确检测到异常状态时，才会执行封禁操作。

---

## 📊 性能优化

### 1. Redis 缓存

- **缓存键名**: `user_status:result:{user_id}`
- **TTL**: 默认 1 小时（可配置）
- **缓存内容**: 检测结果（is_problematic + reason + checked_at）

### 2. 分布式锁

- **锁键名**: `user_status:lock:{user_id}`
- **TTL**: 10 秒（防止死锁）
- 避免并发请求重复检测同一用户

### 3. 重试机制

- 遇到 FloodWaitError 自动重试（最多 10 秒等待）
- 最大重试次数：2 次（可配置）
- 指数退避策略

---

## 🔍 日志示例

### 正常用户

```
[INFO] 用户状态检查: [用户:123456] → 正常
```

### 异常用户（加入请求）

```
[INFO] 异常用户加入请求被拒 [群组:-1001234567890] [用户:123456] [状态:scam]
```

### 异常用户（发送消息）

```
[INFO] 拦截异常用户 [群组:-1001234567890] [用户:123456] [原因:user_status_restricted] [详情:{'status': 'restricted'}] [缓存:False]
```

### API 失败（降级放行）

```
[DEBUG] 用户状态检查异常 [用户:123456]: User not found
```

---

## 🛡️ 安全说明

### 1. Session 文件安全

- `user_bot.session` 文件包含登录凭据，**切勿泄露**
- Docker 部署时通过 volume 挂载，确保文件权限正确：
  ```bash
  chmod 600 data/user_bot.session
  ```

### 2. API 速率限制

Telethon 操作受 Telegram 速率限制约束：
- 单个用户检测：约 20 次/秒
- 触发 FloodWait 后自动等待重试

### 3. 审计日志

所有封禁操作均记录到审计日志：

```sql
SELECT * FROM audit_logs 
WHERE action LIKE 'user_status_ban%' 
ORDER BY created_at DESC;
```

---

## 🆚 与 CAS 的区别

| 特性 | CAS 黑名单 | 用户状态检测 |
|------|-----------|-------------|
| 数据源 | 第三方黑名单 API | Telegram 官方标记 |
| 检测对象 | 垃圾/违规用户 | restricted/scam/fake/deleted |
| 覆盖范围 | 跨群组全局 | Telegram 全平台 |
| 误报率 | 极低 | 极低（官方标记）|
| API 依赖 | CAS API | Telethon |
| 启用条件 | 配置 CAS API | 配置 Telethon 客户端 |
| 推荐场景 | 所有群组 | 高价值群组 |

**建议**：同时启用 CAS 和用户状态检测，获得双重保护。

---

## ⚙️ 故障排查

### 问题 1：用户状态检测未生效

**检查清单**：

1. Telethon 是否启用？
   ```bash
   grep TELETHON_ENABLED .env
   # 应显示 TELETHON_ENABLED=true
   ```

2. Session 文件是否存在？
   ```bash
   ls -la data/user_bot.session
   ```

3. 用户状态检测是否启用？
   ```bash
   grep USER_STATUS_CHECK_ENABLED .env
   # 应显示 USER_STATUS_CHECK_ENABLED=true
   ```

4. 查看启动日志：
   ```bash
   docker-compose logs bot | grep "用户状态"
   # 应显示：✅ 用户状态服务已初始化
   ```

### 问题 2：FloodWaitError 频繁触发

**原因**：短时间内检测了大量用户，触发 Telegram 速率限制。

**解决方案**：

1. 增加缓存时间：
   ```bash
   USER_STATUS_CACHE_TTL=7200  # 2 小时
   ```

2. 避免批量检测：群组人数较多时，慎用 cleanup 功能

### 问题 3：Telethon 连接失败

**检查网络**：

```bash
# 测试 Telegram API 连通性
curl -I https://api.telegram.org
```

**配置代理**（如果需要）：

在 `.env` 中添加：

```bash
# 仅代理 Telegram（Telethon）流量：socks5_proxy 仅被 Bot 用作代理，httpx 会忽略该键，AI API 等其它 HTTP 请求不受影响（all_proxy 则会被 httpx 全局采用）
# Docker 部署不能用 127.0.0.1（指向容器自身）；Linux 原生 Docker 无 host.docker.internal 时改用宿主机 bridge 地址（如 172.17.0.1）
socks5_proxy=socks5://host.docker.internal:1080
```

---

## 📝 更新日志

### v1.3.0 (2026-06-10)

- ✅ 新增用户状态检测功能（基于 Telethon）
- ✅ 支持检测 restricted/scam/fake/deleted 用户
- ✅ 集成到 CAS 中间件和验证流程
- ✅ 添加 Redis 缓存和分布式锁
- ✅ 实现降级策略和重试机制

---

## 🔗 相关文档

- [Telethon 官方文档](https://docs.telethon.dev/)
- [Telegram User 对象](https://core.telegram.org/type/User)
