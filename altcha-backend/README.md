# ALTCHA PHP 后端部署指南

## 概述

ALTCHA 是一个开源的 Proof-of-Work (PoW) CAPTCHA 解决方案，无需第三方服务。本后端提供挑战生成和验证功能。

## 功能特点

- ✅ 完全开源，无外部依赖
- ✅ 隐私友好，不收集用户数据
- ✅ Proof-of-Work 机制，客户端计算验证
- ✅ HMAC 签名验证，安全可靠
- ✅ CORS 支持，可与 Cloudflare WebApp 集成

## 前置要求

- PHP 7.4+ (推荐 PHP 8.0+)
- Composer（用于安装依赖）
- 免费 PHP 主机（推荐 Serv00）

## 部署到 Serv00

### 1. 注册 Serv00 账号

访问 https://www.serv00.com/ 注册免费账号

### 2. SSH 登录

```bash
ssh your-username@s1.serv00.net
```

### 3. 创建项目目录

```bash
cd domains/your-domain.serv00.net/public_html
mkdir altcha
cd altcha
```

### 4. 上传代码

通过 SFTP 或 Git 上传以下文件：
- `composer.json`
- `challenge.php`
- `verify.php`
- `config.php.example`

### 5. 配置密钥

```bash
# 生成配置文件
cp config.php.example config.php

# 编辑配置
nano config.php
```

配置项说明：
```php
// ALTCHA HMAC 密钥（生成命令：openssl rand -hex 32）
define('ALTCHA_HMAC_KEY', '<64字符hex密钥>');

// Bot 签名密钥（必须与 Bot 端 CAPTCHA_SIGNATURE_KEY 一致）
define('BOT_SIGNATURE_KEY', '<64字符hex密钥>');

// 允许的 CORS 域名
define('ALLOWED_ORIGIN', 'https://captcha.your-domain.pages.dev');

// 挑战难度（可选调整）
define('POW_MAX_NUMBER', 100000);  // 工作量
define('POW_EXPIRES', 300);        // 有效期（秒）
```

### 6. 安装依赖

```bash
# 使用 Serv00 提供的 Composer
composer install --no-dev --optimize-autoloader
```

### 7. 设置文件权限

```bash
chmod 644 *.php
chmod 600 config.php  # 保护配置文件
chmod 755 vendor/
```

### 8. 配置 Nginx/Apache

#### 对于 Nginx (Serv00 默认)

Serv00 自动处理，无需额外配置

#### 对于 Apache

创建 `.htaccess`：
```apache
<IfModule mod_rewrite.c>
    RewriteEngine On

    # 允许 PHP 文件直接访问
    RewriteCond %{REQUEST_FILENAME} !-f
    RewriteCond %{REQUEST_FILENAME} !-d
    RewriteRule . /index.php [L]
</IfModule>

# 保护配置文件
<Files config.php>
    Order allow,deny
    Deny from all
</Files>
```

### 9. 验证部署

访问以下 URL 测试：

**测试挑战生成：**
```
https://your-domain.serv00.net/altcha/challenge.php
```

预期响应：
```json
{
  "success": true,
  "challenge": {
    "algorithm": "SHA-256",
    "challenge": "...",
    "salt": "...",
    "signature": "..."
  }
}
```

**测试验证端点（使用 curl）：**
```bash
curl -X POST https://your-domain.serv00.net/altcha/verify.php \
  -H "Content-Type: application/json" \
  -d '{"payload":"test","chat_id":123,"user_id":456,"verify_token":"test"}'
```

预期响应（payload 无效时）：
```json
{
  "success": false,
  "error": "Invalid solution or challenge expired"
}
```

## 配置 Bot 端

在 Bot 的 `.env` 文件中添加：

```bash
# 启用 ALTCHA
ALTCHA_ENABLED=true

# ALTCHA API 地址（不要包含文件名）
ALTCHA_API_URL=https://your-domain.serv00.net/altcha

# ALTCHA HMAC 密钥（与 PHP 后端 ALTCHA_HMAC_KEY 一致）
ALTCHA_HMAC_KEY=<64字符hex密钥>

# 统一 CAPTCHA 签名密钥（与 PHP 后端 BOT_SIGNATURE_KEY 一致）
CAPTCHA_SIGNATURE_KEY=<64字符hex密钥>
```

## API 端点

### GET /challenge.php

生成 Proof-of-Work 挑战

**响应**：
```json
{
  "success": true,
  "challenge": {
    "algorithm": "SHA-256",
    "challenge": "abcd1234",
    "salt": "xyz789",
    "signature": "hmac..."
  }
}
```

### POST /verify.php

验证挑战解答并生成签名

**请求**：
```json
{
  "payload": "altcha_solution",
  "chat_id": 123456,
  "user_id": 789012,
  "verify_token": "abc123def456"  # gitleaks:allow - 示例数据
}
```

**成功响应**：
```json
{
  "success": true,
  "signature": "hmac_sha256_signature",
  "timestamp": 1234567890
}
```

**失败响应**：
```json
{
  "success": false,
  "error": "Invalid solution or challenge expired"
}
```

## 安全注意事项

### 1. 保护配置文件

```bash
# 确保 config.php 不可被 Web 访问
chmod 600 config.php
```

### 2. CORS 配置

只允许你的 WebApp 域名访问：
```php
define('ALLOWED_ORIGIN', 'https://captcha.your-domain.pages.dev');
```

### 3. 密钥管理

- 使用强随机密钥（至少 32 字节）
- 不要将密钥提交到 Git
- 定期轮换密钥（更新后需同步到 Bot 端）

### 4. 日志安全

生产环境关闭调试模式：
```php
define('DEBUG_MODE', false);
```

## 故障排查

### 问题 1: 500 Internal Server Error

**可能原因**：
- Composer 依赖未安装
- PHP 版本不兼容

**解决方法**：
```bash
composer install
php -v  # 检查 PHP 版本
```

### 问题 2: CORS 错误

**现象**：浏览器控制台显示 CORS 错误

**解决方法**：
1. 检查 `ALLOWED_ORIGIN` 配置是否正确
2. 确认 Serv00 未阻止 CORS 头

### 问题 3: 验证失败

**现象**：`{"success": false, "error": "Invalid solution or challenge expired"}`

**排查步骤**：
1. 检查 `ALTCHA_HMAC_KEY` 是否一致
2. 确认挑战未过期（默认 5 分钟）
3. 查看错误日志：`tail -f error_log`

### 问题 4: 签名验证失败（Bot 端）

**现象**：Bot 日志显示 "签名验证失败"

**解决方法**：
- 确认 `BOT_SIGNATURE_KEY` 与 Bot 端 `CAPTCHA_SIGNATURE_KEY` 一致
- 检查时间戳是否在有效范围内（5 分钟）

## 性能优化

### 1. 启用 OPcache

编辑 `php.ini`：
```ini
opcache.enable=1
opcache.memory_consumption=128
opcache.max_accelerated_files=10000
```

### 2. Composer 优化

```bash
composer dump-autoload --optimize --classmap-authoritative
```

### 3. 调整难度

如果用户设备性能较差，降低 `POW_MAX_NUMBER`：
```php
define('POW_MAX_NUMBER', 50000);  // 降低难度
```

## 监控和维护

### 查看访问日志

```bash
tail -f ~/domains/your-domain.serv00.net/logs/access.log
```

### 查看错误日志

```bash
tail -f ~/domains/your-domain.serv00.net/logs/error.log
```

### 定期更新依赖

```bash
composer update altcha-org/altcha
```

## 相关链接

- [ALTCHA 官方文档](https://altcha.org/docs/)
- [ALTCHA PHP 库](https://github.com/altcha-org/altcha-lib-php)
- [Serv00 文档](https://wiki.serv00.com/)

## 技术支持

如遇问题，请查阅：
1. ALTCHA 官方 GitHub Issues
2. Serv00 官方 Wiki
3. Bot 项目 GitHub Issues
