# 统一 CAPTCHA WebApp 部署指南

## 概述

这是一个统一的 Telegram WebApp，支持多种 CAPTCHA 验证服务：
- **Cloudflare Turnstile** - 无感验证（已集成）
- **Friendly Captcha** - 隐私友好，支持多 key 轮换
- **hCaptcha** - 图片验证
- **MTCaptcha** - 自适应验证
- **ALTCHA** - 开源 PoW 验证（需要独立 PHP 后端）

## 部署到 Cloudflare Pages

### 1. 前置要求

- Cloudflare 账号
- Wrangler CLI（可选）：`npm install -g wrangler`

### 2. 部署步骤

#### 方法 A: 使用 Cloudflare Dashboard（推荐）

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. 进入 **Pages** → **Create a project**
3. 连接你的 GitHub/GitLab 仓库
4. 配置构建设置：
   - **Build command**: 留空
   - **Build output directory**: `.`
   - **Root directory**: `captcha-webapp`
5. 点击 **Save and Deploy**

#### 方法 B: 使用 Wrangler CLI

```bash
cd captcha-webapp
wrangler pages deploy . --project-name=tg-guard-captcha
```

### 3. 配置环境变量

在 Cloudflare Dashboard 中配置：

**Settings** → **Environment variables** → **Production**

#### 必需变量（所有 provider）

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `SIGNATURE_KEY` | 与 Bot 共享的签名密钥（64字符hex） | 使用 `openssl rand -hex 32` 生成 |

#### Turnstile 配置

| 变量名 | 说明 |
|--------|------|
| `TURNSTILE_SITE_KEY` | Turnstile Site Key |
| `TURNSTILE_SECRET_KEY` | Turnstile Secret Key |

获取地址：https://dash.cloudflare.com/?to=/:account/turnstile

#### Friendly Captcha 配置

| 变量名 | 说明 | 格式 |
|--------|------|------|
| `FRIENDLY_KEYS` | JSON 数组（支持多 key 轮换） | `[{"sitekey":"FCMAV...","apikey":"fc-sk-..."}]` |

获取地址：https://friendlycaptcha.com/

**示例**：
```json
[
  {"sitekey":"FCMAV1234567890","apikey":"fc-sk-abc123"},
  {"sitekey":"FCMAV0987654321","apikey":"fc-sk-xyz789"}
]
```

#### hCaptcha 配置

| 变量名 | 说明 |
|--------|------|
| `HCAPTCHA_SITE_KEY` | hCaptcha Site Key |
| `HCAPTCHA_SECRET_KEY` | hCaptcha Secret Key |

获取地址：https://www.hcaptcha.com/

#### MTCaptcha 配置

| 变量名 | 说明 |
|--------|------|
| `MTCAPTCHA_SITE_KEY` | MTCaptcha Site Key |
| `MTCAPTCHA_PRIVATE_KEY` | MTCaptcha Private Key |

获取地址：https://www.mtcaptcha.com/

#### ALTCHA 配置

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `ALTCHA_API_URL` | ALTCHA PHP 后端地址 | `https://xxx.serv00.net/altcha` |

注意：ALTCHA 需要单独部署 PHP 后端（见 `../altcha-backend/README.md`）

### 4. 验证部署

部署成功后，访问以下 URL 测试：

```
https://your-site.pages.dev/
https://your-site.pages.dev/api/config?provider=turnstile
https://your-site.pages.dev/api/config?provider=friendly&key_index=0
```

应该看到正常的页面和 JSON 响应。

## 使用方式

### URL 参数

| 参数 | 必需 | 说明 | 示例 |
|------|------|------|------|
| `provider` | ✅ | 验证服务类型 | `turnstile`/`friendly`/`hcaptcha`/`mtcaptcha`/`altcha` |
| `chat_id` | ✅ | Telegram 群组 ID | `123456789` |
| `user_id` | ✅ | Telegram 用户 ID | `987654321` |
| `token` | ✅ | 一次性验证 token | `abc123...` |
| `key_index` | ⬜ | Friendly Captcha key 索引（默认 0） | `0`/`1`/`2` |

### 示例 URL

```
https://your-site.pages.dev/?provider=friendly&chat_id=123&user_id=456&token=abc123&key_index=0
```

## API 端点

### GET /api/config

获取 provider 配置（site_key 等）

**请求**：
```
GET /api/config?provider=friendly&key_index=0
```

**响应**：
```json
{
  "success": true,
  "site_key": "FCMAV1234567890"
}
```

### POST /api/verify

验证 CAPTCHA 响应并生成签名

**请求**：
```json
{
  "provider": "friendly",
  "captcha_response": "solution_string",
  "chat_id": 123,
  "user_id": 456,
  "verify_token": "abc123",
  "key_index": 0
}
```

**响应**：
```json
{
  "success": true,
  "signature": "hmac_signature",
  "timestamp": 1234567890
}
```

## 安全说明

1. **SIGNATURE_KEY** 必须与 Bot 端的 `CAPTCHA_SIGNATURE_KEY` 一致
2. 所有敏感密钥都应在 Cloudflare Dashboard 中配置，**不要提交到 Git**
3. HMAC 签名使用 SHA-256 算法，消息格式：`{chat_id}:{user_id}:{verify_token}:{timestamp}`
4. 验证 token 由 Bot 生成，存储在 Redis 中，使用后失效

## 故障排查

### 问题 1: 配置 API 返回错误

**现象**：`{"success": false, "error": "XXX not configured"}`

**解决**：检查 Cloudflare Dashboard 中的环境变量是否正确配置

### 问题 2: 验证失败

**现象**：`{"success": false, "error": "Verification failed"}`

**可能原因**：
- CAPTCHA 响应无效或过期
- Secret Key 配置错误
- 验证服务 API 不可用

**排查方法**：查看 Cloudflare Pages Functions 日志

### 问题 3: CORS 错误

**现象**：浏览器控制台显示 CORS 错误

**解决**：Cloudflare Pages Functions 默认支持 CORS，如需自定义，修改 API 文件添加响应头

### 问题 4: Friendly Captcha key 轮换不工作

**现象**：总是使用第一个 key

**解决**：
1. 检查 `FRIENDLY_KEYS` 是否为有效的 JSON 数组
2. 确认 Bot 端正确传递了 `key_index` 参数

## 更新部署

### 自动部署

如果使用 Git 集成部署，每次 push 到仓库会自动触发部署。

### 手动部署

```bash
cd captcha-webapp
wrangler pages deploy . --project-name=tg-guard-captcha
```

### 回滚

在 Cloudflare Dashboard 中：

**Pages** → **你的项目** → **Deployments** → 选择历史版本 → **Rollback**

## 相关文档

- [Cloudflare Pages 文档](https://developers.cloudflare.com/pages/)
- [Cloudflare Pages Functions](https://developers.cloudflare.com/pages/functions/)
- [Telegram WebApp 文档](https://core.telegram.org/bots/webapps)
- [Turnstile 文档](https://developers.cloudflare.com/turnstile/)
- [Friendly Captcha 文档](https://docs.friendlycaptcha.com/)
- [hCaptcha 文档](https://docs.hcaptcha.com/)
- [MTCaptcha 文档](https://www.mtcaptcha.com/dev-guide)
- [ALTCHA 文档](https://altcha.org/docs/)
