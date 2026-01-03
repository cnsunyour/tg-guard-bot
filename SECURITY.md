# 安全策略 (Security Policy)

## 🔒 支持的版本

当前仅维护最新版本的安全更新：

| 版本 | 支持状态 |
| --- | --- |
| 0.1.x | ✅ 当前支持 |

## 🚨 报告安全漏洞

如果您发现安全漏洞，请**不要**通过公开 issue 报告。

### 报告流程

1. **私密报告**：通过 GitHub Security Advisories 或直接联系维护者
2. **提供信息**：
   - 漏洞类型和严重程度
   - 复现步骤
   - 影响范围
   - 建议的修复方案（如有）
3. **响应时间**：
   - 确认收到：24 小时内
   - 初步评估：48 小时内
   - 修复计划：7 天内

### 负责任披露政策

- 在修复发布前，请勿公开披露漏洞
- 我们承诺在 30 天内发布安全补丁
- 修复后，我们会在 Release Notes 中致谢报告者（如您同意）

---

## 🛡️ 已实施的安全措施

### 认证与授权
- ✅ 多层权限验证（超级管理员 / 群管理员）
- ✅ Callback 上下文绑定验证
- ✅ 所有敏感操作强制权限检查

### 输入验证
- ✅ 用户 ID 范围验证（防止整数溢出）
- ✅ 文件路径白名单验证（防止路径遍历）
- ✅ 参数白名单验证
- ✅ 时长上限验证（最大 366 天）

### 数据保护
- ✅ 敏感信息日志脱敏
- ✅ HTML 注入防护（所有用户输入转义）
- ✅ 强密码策略强制执行
- ✅ ML 模型文件 HMAC-SHA256 签名验证

### 加密与随机数
- ✅ 使用 `secrets` 模块生成加密安全随机数
- ✅ Redis 密码认证
- ✅ PostgreSQL 密码保护

### DoS 防护
- ✅ 速率限制中间件（消息：3/秒，回调：5/秒）
- ✅ 时长解析上限限制
- ✅ 临时文件自动清理

### 运行时安全
- ✅ Docker 容器非 root 用户运行
- ✅ 数据库端口不对外暴露
- ✅ 最小权限原则

### 依赖安全
- ✅ 版本锁定（requirements.txt）
- ✅ 定期安全扫描（Bandit, Safety）
- ✅ GitHub Dependabot 自动更新

---

## 🔍 安全审计记录

### 2025-01-03 - 全面安全审计
- **工具**：Semgrep, Trivy, Bandit, Codex, Gemini
- **发现**：26 个问题（2 关键，8 高危，9 中危，7 低危）
- **修复率**：88.5% (23/26)
- **状态**：所有关键和高危问题已修复

#### 修复详情
- **P0 关键**：Pickle RCE, 回调授权绕过, HTML 注入等 - 已全部修复
- **P1 高危**：日志泄露, 端口暴露, 速率限制等 - 已全部修复
- **P2 中危**：输入验证, 异常处理等 - 已修复 7/9
- **P3 低危**：容器安全, 依赖管理等 - 已修复 6/7

---

## 📋 安全检查清单

### 部署前必检项

#### 环境配置
- [ ] 所有 `.env` 变量已正确设置
- [ ] `BOT_TOKEN` 不是示例值
- [ ] `ADMIN_IDS` 设置正确的管理员 ID
- [ ] 数据库密码强度 ≥16 字符（大小写+数字+特殊字符）
- [ ] Redis 密码强度 ≥16 字符
- [ ] `MODEL_SIGNATURE_KEY` 使用随机生成的 64 字符密钥

#### 网络安全
- [ ] 数据库端口未对外暴露（docker-compose.yml 已移除端口映射）
- [ ] Redis 端口未对外暴露
- [ ] 使用防火墙限制入站连接
- [ ] 考虑使用反向代理（Nginx）

#### 容器安全
- [ ] 容器以非 root 用户运行（user: 1000:1000）
- [ ] Volume 权限正确设置
- [ ] 日志轮转配置正确

#### 应用安全
- [ ] 速率限制已启用
- [ ] 日志级别设为 INFO 或以上（生产环境避免 DEBUG）
- [ ] 所有管理员命令需要权限验证

### 运行时监控
- [ ] 定期检查日志中的安全警告
- [ ] 监控异常速率限制触发
- [ ] 追踪管理员操作审计日志
- [ ] 检查数据库连接数和性能

### 定期维护
- [ ] 每月运行 `safety check` 检查依赖漏洞
- [ ] 每月运行 `bandit -r src/` 扫描代码
- [ ] 每季度更新依赖包到安全版本
- [ ] 每年进行全面安全审计

---

## 🔧 安全配置建议

### 生产环境 .env 配置示例

```bash
# ⚠️ 使用强密码！
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_IDS=123456789

# 数据库（强密码示例）
DB_PASSWORD=$(openssl rand -base64 32)

# Redis（强密码示例）
REDIS_PASSWORD=$(openssl rand -base64 32)

# 模型签名密钥
MODEL_SIGNATURE_KEY=$(openssl rand -hex 32)

# 生产环境日志
LOG_LEVEL=INFO
DEBUG=false

# 速率限制（可根据实际调整）
RATE_LIMIT_MESSAGES=3
RATE_LIMIT_CALLBACKS=5
```

### Docker Compose 生产配置

```yaml
# 资源限制
services:
  bot:
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M
    restart: unless-stopped
    # 只读根文件系统（可选）
    read_only: true
    tmpfs:
      - /tmp
```

---

## 📚 安全参考资源

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [CWE Top 25](https://cwe.mitre.org/top25/)
- [Docker Security Best Practices](https://docs.docker.com/develop/security-best-practices/)
- [Python Security Best Practices](https://python.readthedocs.io/en/stable/library/security_warnings.html)

---

## 📞 联系方式

- **安全问题**：通过 GitHub Security Advisories
- **一般问题**：通过 GitHub Issues
- **紧急漏洞**：直接联系维护者

---

**最后更新**：2025-01-03
**下次审计**：2025-04-03
