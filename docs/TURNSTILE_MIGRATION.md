# Turnstile 迁移指南

## 从独立 Turnstile WebApp 迁移到统一 CAPTCHA WebApp

如果你之前部署了独立的 Turnstile WebApp（`turnstile-webapp/`），现在可以迁移到统一的 CAPTCHA WebApp（`captcha-webapp/`），获得更好的架构和可扩展性。

## 为什么要迁移？

### 统一 WebApp 的优势

- ✅ **统一部署**：所有 CAPTCHA 服务（Turnstile、Friendly、hCaptcha、MTCaptcha）共用一个 WebApp
- ✅ **简化维护**：只需维护一个 WebApp，而不是多个独立部署
- ✅ **更好扩展**：未来新增 CAPTCHA 服务无需额外部署
- ✅ **统一配置**：只需配置一次 `CAPTCHA_WEBAPP_URL` 和 `CAPTCHA_SIGNATURE_KEY`
- ✅ **代码复用**：共享验证逻辑、签名机制、错误处理

### 是否必须迁移？

**不必须**。现有的 Turnstile 独立 WebApp 仍然完全可用，Bot 会自动兼容：
- 如果配置了 `CAPTCHA_WEBAPP_URL`，优先使用统一 WebApp
- 如果只配置了 `TURNSTILE_WEBAPP_URL`，继续使用独立 WebApp
- 两种方式功能完全相同，只是架构不同

## 迁移步骤

### 1. 部署统一 CAPTCHA WebApp

参考 [captcha-webapp/README.md](../captcha-webapp/README.md) 完成部署。

关键步骤：
```bash
# 在 Cloudflare Dashboard 中创建新项目
# 项目名称：tg-guard-captcha
# 构建目录：captcha-webapp
```

### 2. 配置环境变量

在 Cloudflare Dashboard → Settings → Environment variables → Production 中配置：

**必需变量**：
- `SIGNATURE_KEY` - 与 Bot 端的 `CAPTCHA_SIGNATURE_KEY` 一致
- `TURNSTILE_SITE_KEY` - 从旧 WebApp 复制
- `TURNSTILE_SECRET_KEY` - 从旧 WebApp 复制

**可选变量**（如果你想启用其他 CAPTCHA 服务）：
- `FRIENDLY_KEYS`
- `HCAPTCHA_SITE_KEY` / `HCAPTCHA_SECRET_KEY`
- `MTCAPTCHA_SITE_KEY` / `MTCAPTCHA_PRIVATE_KEY`

### 3. 更新 Bot 配置

更新 `.env` 文件：

**新配置（推荐）**：
```env
# 统一 CAPTCHA 配置
CAPTCHA_WEBAPP_URL=https://captcha.your-domain.pages.dev
CAPTCHA_SIGNATURE_KEY=<你的签名密钥>

# Turnstile 启用开关
TURNSTILE_ENABLED=true

# ⚠️ 可选：保留旧配置作为备份，但不会被使用
# TURNSTILE_WEBAPP_URL=https://verify.your-domain.pages.dev
# TURNSTILE_SIGNATURE_KEY=<旧签名密钥>
```

**注意事项**：
- `CAPTCHA_SIGNATURE_KEY` 可以与旧的 `TURNSTILE_SIGNATURE_KEY` 相同或不同
- 如果使用不同的密钥，需要在统一 WebApp 中重新配置
- 建议使用新的密钥，提高安全性

### 4. 重启 Bot

```bash
make prod-restart
# 或
docker-compose restart bot
```

### 5. 测试验证

1. 在测试群组中触发验证流程
2. 确认打开的是新的统一 WebApp（URL 包含 `?provider=turnstile`）
3. 完成验证，确认功能正常

### 6. 清理旧 WebApp（可选）

确认新 WebApp 工作正常后，可以删除旧的独立 Turnstile WebApp：

1. 在 Cloudflare Dashboard 中删除旧项目
2. 从 Bot 配置中删除 `TURNSTILE_WEBAPP_URL` 和 `TURNSTILE_SIGNATURE_KEY`
3. （可选）删除本地的 `turnstile-webapp/` 目录

## 配置对比

### 旧配置（独立 WebApp）

```env
TURNSTILE_ENABLED=true
TURNSTILE_WEBAPP_URL=https://verify.your-domain.pages.dev
TURNSTILE_SIGNATURE_KEY=abc123...
```

### 新配置（统一 WebApp）

```env
# 统一配置（所有 CAPTCHA 服务共用）
CAPTCHA_WEBAPP_URL=https://captcha.your-domain.pages.dev
CAPTCHA_SIGNATURE_KEY=xyz789...

# 启用 Turnstile
TURNSTILE_ENABLED=true

# 可选：启用其他服务
FRIENDLY_ENABLED=true
FRIENDLY_KEYS='[...]'
```

## 向后兼容说明

Bot 端会自动检测配置并选择正确的 WebApp：

```python
# 优先使用统一 WebApp
webapp_base_url = settings.captcha_webapp_url or settings.turnstile_webapp_url

if settings.captcha_webapp_url:
    # 使用统一 WebApp，URL 包含 provider 参数
    url = f"{webapp_base_url}?provider=turnstile&chat_id={chat_id}..."
else:
    # 使用独立 WebApp，URL 不包含 provider 参数
    url = f"{webapp_base_url}?chat_id={chat_id}..."
```

签名密钥也会自动选择：
```python
# 优先使用统一签名密钥
signature_key = settings.captcha_signature_key or settings.turnstile_signature_key
```

## 故障排查

### 问题 1: 验证失败，提示签名错误

**原因**：`CAPTCHA_SIGNATURE_KEY` 与 WebApp 的 `SIGNATURE_KEY` 不一致

**解决**：
1. 检查 Bot 端 `.env` 文件中的 `CAPTCHA_SIGNATURE_KEY`
2. 检查 Cloudflare Dashboard 中的 `SIGNATURE_KEY` 环境变量
3. 确保两者完全一致

### 问题 2: 打开的还是旧 WebApp

**原因**：Bot 端配置了 `TURNSTILE_WEBAPP_URL` 但没有配置 `CAPTCHA_WEBAPP_URL`

**解决**：
1. 检查 `.env` 文件，确认 `CAPTCHA_WEBAPP_URL` 已配置
2. 重启 Bot：`make prod-restart`

### 问题 3: 配置 API 返回错误

**原因**：Cloudflare WebApp 环境变量未配置

**解决**：
1. 登录 Cloudflare Dashboard
2. 进入你的 Pages 项目 → Settings → Environment variables
3. 确认 `TURNSTILE_SITE_KEY` 和 `TURNSTILE_SECRET_KEY` 已配置
4. 重新部署 WebApp

## FAQ

### Q: 迁移后需要更换 Turnstile Site Key 吗？

**A**: 不需要。统一 WebApp 使用相同的 Turnstile Site Key 和 Secret Key。

### Q: 可以同时保留两个 WebApp 吗？

**A**: 可以，但不推荐。Bot 会优先使用 `CAPTCHA_WEBAPP_URL`，`TURNSTILE_WEBAPP_URL` 只作为备份。

### Q: 迁移需要停机吗？

**A**: 不需要。可以先部署新 WebApp，测试无误后再切换配置，实现零停机迁移。

### Q: 如果新 WebApp 出问题，如何快速回滚？

**A**: 保留旧配置，只需注释掉 `CAPTCHA_WEBAPP_URL`，重启 Bot 即可回滚：
```env
# CAPTCHA_WEBAPP_URL=https://captcha.your-domain.pages.dev
TURNSTILE_WEBAPP_URL=https://verify.your-domain.pages.dev  # 回滚到旧 WebApp
```

### Q: 其他 CAPTCHA 服务需要单独迁移吗？

**A**: 不需要。Friendly、hCaptcha、MTCaptcha 从设计之初就使用统一 WebApp，无需迁移。

## 推荐的迁移时间表

| 阶段 | 时间 | 操作 |
|------|------|------|
| 准备阶段 | 第 1 天 | 部署统一 WebApp，配置环境变量 |
| 测试阶段 | 第 2-3 天 | 在测试群组中验证功能 |
| 灰度发布 | 第 4-5 天 | 小范围生产环境测试 |
| 全量切换 | 第 6 天 | 更新所有 Bot 配置 |
| 清理阶段 | 第 7+ 天 | 确认稳定后删除旧 WebApp |

## 技术支持

如遇问题，请：
1. 查看 Bot 日志：`make prod-logs`
2. 查看 Cloudflare Functions 日志
3. 提交 Issue：[GitHub Issues](https://github.com/cnsunyour/tg-guard-bot/issues)
