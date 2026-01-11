# Turnstile WebApp - Telegram Guard Bot 人机验证

这是一个独立部署的 WebApp 项目，用于为 Telegram Guard Bot 提供 Cloudflare Turnstile 人机验证功能。

## 📋 功能说明

- **前端**: Turnstile Widget + Telegram WebApp SDK 集成
- **后端**: Cloudflare Pages Functions (Serverless API)
- **安全**: HMAC-SHA256 签名 + 一次性 Token 防重放
- **部署**: Cloudflare Pages 免费托管 + 全球 CDN

## 🚀 快速部署

### 前置要求

1. **Cloudflare 账号**: 注册 https://dash.cloudflare.com/
2. **Turnstile 密钥**: 获取 Site Key 和 Secret Key
3. **Wrangler CLI**: 安装 Cloudflare 部署工具

### 步骤 1: 安装 Wrangler CLI

```bash
npm install -g wrangler
```

### 步骤 2: 登录 Cloudflare

```bash
wrangler login
```

### 步骤 3: 获取 Turnstile 密钥

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. 进入 **Turnstile** → **Add Widget**
3. 填写信息：
   - **Widget name**: `tg-guard-verification`
   - **Domain**: `*.pages.dev` (或你的自定义域名)
   - **Widget Mode**: `Managed` (推荐) 或 `Non-Interactive`
4. 点击 **Create** 获取：
   - **Site Key** (公开，用于前端)
   - **Secret Key** (保密，用于后端)

### 步骤 4: 生成共享签名密钥

```bash
# Linux/macOS
openssl rand -hex 32

# Python
python -c "import secrets; print(secrets.token_hex(32))"
```

**⚠️ 重要**: 这个密钥需要与 Bot 端的 `TURNSTILE_SIGNATURE_KEY` 保持一致！

### 步骤 5: 部署到 Cloudflare Pages

```bash
# 进入项目目录
cd turnstile-webapp

# 部署
wrangler pages deploy . --project-name=tg-guard-turnstile
```

部署成功后，你会看到类似输出：

```
✨ Success! Uploaded 3 files (1.23 sec)

✨ Deployment complete! Take a peek over at
   https://tg-guard-turnstile.pages.dev
```

**记录这个 URL**，稍后需要配置到 Bot 端！

### 步骤 6: 配置环境变量

1. 打开 [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. 进入 **Workers & Pages** → 找到 `tg-guard-turnstile` 项目
3. 点击 **Settings** → **Environment variables** → **Production**
4. 添加以下变量：

| 变量名 | 值 | 说明 |
|-------|---|------|
| `TURNSTILE_SITE_KEY` | `0x4AAAAAAA...` | Turnstile Site Key (公开) |
| `TURNSTILE_SECRET_KEY` | `0x4AAAAAAA...` | Turnstile Secret Key (保密) |
| `SIGNATURE_KEY` | `<64字符随机密钥>` | 与 Bot 共享的签名密钥 |

5. 点击 **Save**

### 步骤 7: 配置 Bot 端

编辑 Bot 项目的 `.env` 文件：

```bash
# Turnstile 验证配置
TURNSTILE_ENABLED=true
TURNSTILE_WEBAPP_URL=https://tg-guard-turnstile.pages.dev
TURNSTILE_SIGNATURE_KEY=<与 WebApp 相同的 64 字符密钥>
```

### 步骤 8: 重启 Bot

```bash
cd /path/to/tg-guard-bot
make dev-restart
```

### 步骤 9: 测试验证

1. 在 Telegram 群组中发送命令：`/verify turnstile`
2. 邀请测试用户加入群组
3. 用户应看到 **🔐 开始验证** WebApp 按钮
4. 点击按钮 → 完成 Turnstile 验证 → 自动关闭 → 恢复权限

## 🔧 本地开发

### 运行本地开发服务器

```bash
# 安装依赖（如果需要）
npm install

# 启动开发服务器
wrangler pages dev .
```

访问 http://localhost:8788/?chat_id=123&user_id=456&token=test

### 测试 API

```bash
curl -X POST http://localhost:8788/api/verify \
  -H "Content-Type: application/json" \
  -d '{
    "chat_id": "123",
    "user_id": "456",
    "verify_token": "test_token",
    "cf_token": "test_cf_token"
  }'
```

## 📁 项目结构

```
turnstile-webapp/
├── index.html              # 前端页面 (Turnstile Widget + Telegram WebApp)
├── functions/
│   └── api/
│       └── verify.js       # Pages Functions API (验证 + 签名)
├── wrangler.toml           # Cloudflare 配置
└── README.md               # 本文档
```

## 🔒 安全特性

1. **HMAC-SHA256 签名**: 防止数据篡改
2. **一次性 Token**: Redis 存储，验证后立即删除
3. **时间戳验证**: 5 分钟有效期，防重放攻击
4. **Turnstile 验证**: Cloudflare 人机验证，防机器人
5. **环境变量隔离**: 敏感密钥不提交到 Git

## 🌍 自定义域名（可选）

### 绑定自定义域名

1. 在 Cloudflare Pages 项目中点击 **Custom domains**
2. 添加你的域名（如 `verify.example.com`）
3. Cloudflare 会自动配置 DNS 和 SSL 证书
4. 更新 Bot 配置中的 `TURNSTILE_WEBAPP_URL`

## 🐛 故障排查

### 问题 1: "人机验证失败"

**原因**: Turnstile Secret Key 配置错误

**解决**:
1. 检查 Cloudflare Pages 环境变量中的 `TURNSTILE_SECRET_KEY`
2. 确认 `index.html` 中的 `data-sitekey` 与 Dashboard 一致

### 问题 2: "验证参数无效"

**原因**: URL 参数缺失或格式错误

**解决**:
1. 检查 Bot 端的 `TURNSTILE_WEBAPP_URL` 配置
2. 确认 URL 格式正确（不要包含尾部斜杠）

### 问题 3: Bot 收不到回调

**原因**: 签名验证失败

**解决**:
1. 确认 Bot 和 WebApp 的 `SIGNATURE_KEY` 完全一致
2. 检查 Bot 日志: `docker compose logs bot | grep turnstile`

### 问题 4: "验证加载失败"

**原因**: Turnstile 脚本加载失败

**解决**:
1. 检查网络连接
2. 确认域名配置正确（如果使用自定义域名）
3. 尝试切换 Widget Mode (Managed/Non-Interactive)

## 📊 监控和日志

### 查看 Pages Functions 日志

1. 进入 Cloudflare Dashboard
2. **Workers & Pages** → `tg-guard-turnstile`
3. **Logs** → 查看实时请求日志

### Bot 端日志

```bash
# 查看 Turnstile 相关日志
docker compose logs bot | grep -E "turnstile|WebApp"

# 查看错误日志
tail -f logs/error_*.log
```

## 🔄 更新部署

```bash
# 拉取最新代码
git pull origin main

# 进入 WebApp 目录
cd turnstile-webapp

# 重新部署
wrangler pages deploy . --project-name=tg-guard-turnstile
```

## 📚 相关文档

- [Cloudflare Turnstile 官方文档](https://developers.cloudflare.com/turnstile/)
- [Cloudflare Pages Functions](https://developers.cloudflare.com/pages/functions/)
- [Telegram WebApp API](https://core.telegram.org/bots/webapps)
- [Wrangler CLI](https://developers.cloudflare.com/workers/wrangler/)

## 💡 提示

- Cloudflare Pages 免费额度：每月 100,000 次请求
- Turnstile 免费额度：20 个 Widget，无限请求
- WebApp 自动部署到全球 CDN，访问速度快
- 支持 GitHub/GitLab 自动部署（可选）

## 📝 许可

MIT License - 自由使用和修改
