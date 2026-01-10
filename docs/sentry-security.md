# Sentry 安全配置指南

## 🚨 问题说明

在使用 Sentry 收集错误日志时，可能会遇到敏感信息（如 Bot Token）泄露到错误消息中的问题。例如：

```
Failed to fetch updates - TelegramNetworkError: HTTP Client says -
ClientConnectorError: Cannot connect to host api.telegram.org:443
ssl:default [None]
```

错误的详细堆栈中可能包含完整的 API 请求 URL，包括 Bot Token：
```
https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz/getUpdates
```

## ✅ 解决方案

### 1. 自动过滤敏感数据

项目已配置 `before_send` 钩子自动过滤所有包含 Bot Token 的内容：

**过滤规则**：
- 识别 Telegram Bot Token 格式：`\d+:[A-Za-z0-9_-]{35}`
- 识别 URL 中的 Token：`bot(\d+:[A-Za-z0-9_-]{35})`
- 替换为：`[FILTERED_BOT_TOKEN]`

**过滤范围**：
- 异常消息（exception）
- 日志消息（message）
- 面包屑（breadcrumbs）
- 请求信息（request）
- 额外上下文（extra）

### 2. 测试过滤功能

运行测试脚本验证过滤功能：

```bash
python test_sentry.py
```

**预期输出**：
```
过滤前的消息: Failed to fetch from https://api.telegram.org/bot123456789:ABC.../getUpdates
过滤后的消息: Failed to fetch from https://api.telegram.org/bot[FILTERED_BOT_TOKEN]/getUpdates

✅ 敏感数据过滤测试通过！Token 已被正确过滤
```

### 3. 验证线上过滤效果

1. 在 Sentry 控制台查看最新的错误事件
2. 检查错误消息和堆栈跟踪
3. 确认所有 Token 都显示为 `[FILTERED_BOT_TOKEN]`

---

## 🔒 已泄露 Token 的处理

### ⚠️ 紧急操作

如果 Token 已经泄露到 Sentry 中，**必须立即**执行以下操作：

#### 1️⃣ **立即更换 Bot Token**

```bash
# 1. 与 @BotFather 对话
/mybots
# 2. 选择你的 bot
# 3. Bot Settings -> API Token -> Revoke current token
# 4. 确认撤销旧 Token
# 5. 获取新 Token
```

#### 2️⃣ **更新配置**

```bash
# 更新 .env 文件
BOT_TOKEN=新的Token

# 重启服务
make dev-restart  # 开发环境
# 或
make prod-restart # 生产环境
```

#### 3️⃣ **清理 Sentry 历史事件**

**方法一：删除单个事件**
1. 登录 Sentry 控制台
2. 进入 Issues 页面
3. 找到包含 Token 的事件
4. 点击右上角 "..." 菜单
5. 选择 "Delete" 删除事件

**方法二：批量删除事件**
1. 进入 Project Settings
2. 点击 "Data" 标签
3. 在 "Event Retention" 部分
4. 选择 "Delete and Discard"
5. 设置删除时间范围
6. 确认批量删除

**方法三：使用 API 删除（推荐）**

```bash
# 安装 Sentry CLI
npm install -g @sentry/cli

# 配置认证
export SENTRY_AUTH_TOKEN=你的Sentry_Auth_Token
export SENTRY_ORG=你的组织名
export SENTRY_PROJECT=你的项目名

# 删除特定时间范围的事件
sentry-cli issues delete --status unresolved --before "2026-01-10"
```

#### 4️⃣ **审计访问记录**

1. 进入 Sentry Project Settings -> "Team Access"
2. 查看哪些人有权访问该项目
3. 移除不必要的访问权限
4. 如果需要，更换所有相关凭证

---

## 🛡️ 预防措施

### 1. 环境隔离

生产环境和开发环境使用不同的 Bot：

```bash
# .env.production
BOT_TOKEN=生产环境Token
SENTRY_ENVIRONMENT=production

# .env.development
BOT_TOKEN=开发环境Token
SENTRY_ENVIRONMENT=development
```

### 2. 访问控制

**Sentry 项目权限最小化**：
- 仅授权必要的团队成员
- 使用角色权限（Viewer/Developer/Admin）
- 定期审计访问日志

### 3. 敏感数据检查

**Sentry 配置**：
```python
sentry_sdk.init(
    dsn=settings.sentry_dsn,
    send_default_pii=False,  # ✅ 禁止发送个人身份信息
    before_send=before_send,  # ✅ 自定义过滤钩子
)
```

### 4. 日志脱敏

在记录日志时避免包含敏感信息：

```python
# ❌ 错误做法
logger.error(f"Failed to call {full_url}")

# ✅ 正确做法
logger.error(f"Failed to call Telegram API: {endpoint}")
```

### 5. 监控告警

设置 Sentry 告警规则：
1. 进入 Project Settings -> Alerts
2. 创建新规则
3. 配置触发条件（如：错误率突增）
4. 设置通知渠道（邮件/Slack/钉钉）

---

## 📊 安全检查清单

部署前检查：

- [ ] 已启用 Sentry 敏感数据过滤（`before_send` 钩子）
- [ ] 已禁用 PII 发送（`send_default_pii=False`）
- [ ] 已运行过滤测试（`python test_sentry.py`）
- [ ] 生产和开发环境使用不同的 Bot Token
- [ ] Sentry 项目访问权限已最小化
- [ ] 已配置 Sentry 告警规则
- [ ] 已设置环境变量隔离（`.env.production` / `.env.development`）
- [ ] 代码中不包含硬编码的敏感信息

部署后验证：

- [ ] 在 Sentry 控制台查看测试事件
- [ ] 确认所有 Token 显示为 `[FILTERED_BOT_TOKEN]`
- [ ] 验证错误消息不包含敏感 URL
- [ ] 检查堆栈跟踪中无敏感信息

---

## 🔍 常见问题

### Q1: 过滤后的错误如何调试？

**A**: 过滤不影响本地日志，本地开发时查看 `logs/bot_*.log` 文件即可。生产环境的错误调试应通过堆栈跟踪和上下文信息进行，而非依赖敏感数据。

### Q2: 如何确认过滤功能生效？

**A**:
1. 运行 `python test_sentry.py`
2. 选择发送测试事件
3. 在 Sentry 控制台查看事件详情
4. 确认 Token 显示为 `[FILTERED_BOT_TOKEN]`

### Q3: 已有的错误事件会被自动过滤吗？

**A**: 不会。`before_send` 只对新事件生效。已有的事件需要手动删除（参考"已泄露 Token 的处理"章节）。

### Q4: 过滤会影响性能吗？

**A**: 影响极小。正则替换在事件发送前执行，不影响应用主逻辑。测试显示开销 < 1ms。

### Q5: 可以过滤其他敏感信息吗？

**A**: 可以。修改 `src/main.py` 中的 `before_send` 函数，添加新的正则模式即可。例如：

```python
# 过滤数据库密码
db_password_pattern = re.compile(r"password=[^&\s]+")
data = db_password_pattern.sub("password=[FILTERED]", data)

# 过滤 IP 地址
ip_pattern = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
data = ip_pattern.sub("[FILTERED_IP]", data)
```

---

## 📚 参考资料

- [Sentry Data Scrubbing](https://docs.sentry.io/product/data-management-settings/scrubbing/)
- [Sentry before_send Hook](https://docs.sentry.io/platforms/python/configuration/filtering/)
- [Telegram Bot Security Best Practices](https://core.telegram.org/bots#3-how-do-i-create-a-bot)
- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)

---

**最后更新**: 2026-01-10
**版本**: v1.0
**适用于**: tg-guard-bot with Sentry integration
