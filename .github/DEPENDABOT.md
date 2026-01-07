# Dependabot 配置说明

## 📋 概述

本项目使用 Dependabot 自动监控和更新依赖项，确保项目使用最新的安全版本。

## 🔧 配置文件

- `.github/dependabot.yml` - Dependabot 主配置
- `.github/workflows/dependabot-auto-merge.yml` - 自动合并工作流

## 📦 监控的依赖类型

### 1. Python 依赖（pip）
- **更新频率**: 每周一 02:00 (Asia/Shanghai)
- **PR 限制**: 最多 10 个
- **分组策略**:
  - `security-updates`: 安全更新（自动合并）
  - `major-updates`: 主要版本更新（需审查）
  - `minor-and-patch`: 次要版本和补丁（自动批准）

**特殊处理**:
- `paddleocr` / `paddlepaddle`: 锁定在 2.x（ARM 兼容性）

### 2. Docker 镜像
- **更新频率**: 每周一 03:00 (Asia/Shanghai)
- **PR 限制**: 最多 5 个
- **策略**: 仅监控次要版本和补丁（Python 3.12.x）

### 3. GitHub Actions
- **更新频率**: 每周一 04:00 (Asia/Shanghai)
- **PR 限制**: 最多 5 个
- **策略**: 分组更新所有 Actions

## 🤖 自动合并策略

### 自动批准
- ✅ 安全更新（security）
- ✅ 补丁更新（patch）

### 自动合并（需通过测试）
- ✅ 关键库的安全补丁更新：
  - `aiohttp`
  - `aiogram`
  - `sqlalchemy`

### 需要手动审查
- ⚠️ 主要版本更新（major）
- ⚠️ 次要版本更新（minor）

## 📊 PR 标签说明

| 标签 | 说明 | 处理方式 |
|------|------|---------|
| `dependencies` | 依赖更新 | 自动添加 |
| `python` | Python 依赖 | 自动添加 |
| `security` | 安全更新 | 高优先级，自动合并 |
| `major-update` | 主要版本 | 需要手动审查和测试 |
| `minor-update` | 次要版本 | 建议审查后合并 |
| `needs-review` | 需要审查 | 手动处理 |

## 🔒 安全检查

所有 Dependabot PR 必须通过以下检查才能合并：

1. **Bandit 代码扫描** - 必须通过
2. **pip-audit 漏洞检查** - 警告不阻塞
3. **pytest 测试** - 警告不阻塞

## 🎯 最佳实践

### 审查 PR 时的检查清单

- [ ] 查看更新日志（CHANGELOG）了解变更内容
- [ ] 检查是否有破坏性变更（Breaking Changes）
- [ ] 运行本地测试确保功能正常
- [ ] 检查安全扫描结果
- [ ] 主要版本更新需要在 dev 分支测试

### 手动触发 Dependabot

```bash
# 在 GitHub 仓库页面
# Settings → Code security and analysis → Dependabot version updates
# 点击 "Check for updates" 按钮
```

### 临时禁用特定依赖更新

编辑 `.github/dependabot.yml`，添加到 `ignore` 列表：

```yaml
ignore:
  - dependency-name: "package-name"
    update-types: ["version-update:semver-major"]
```

## 📈 监控和报告

### 查看 Dependabot 状态

1. **仓库首页** → Insights → Dependency graph → Dependabot
2. **Security** → Dependabot alerts（查看安全警报）
3. **Pull requests** → 标签过滤：`dependencies`

### 接收通知

- **GitHub 通知**: 默认启用（PR 创建、合并、评论）
- **邮件通知**: Settings → Notifications → Dependabot alerts

## ⚙️ 仓库设置要求

在 GitHub 仓库中启用以下功能：

1. **Settings → Code security and analysis**:
   - ✅ Dependency graph
   - ✅ Dependabot alerts
   - ✅ Dependabot security updates
   - ✅ Dependabot version updates

2. **Settings → Branches → Branch protection rules (main/dev)**:
   - ✅ Require status checks to pass before merging
   - ✅ Require branches to be up to date before merging

3. **Settings → Actions → General**:
   - ✅ Allow all actions and reusable workflows

## 🚀 启用步骤

1. **合并此 PR** - 将 Dependabot 配置合并到 main/dev 分支

2. **启用 Dependabot**:
   ```
   GitHub 仓库 → Settings → Code security and analysis
   → Enable Dependabot version updates
   ```

3. **验证配置**:
   - 查看 Insights → Dependency graph → Dependabot
   - 等待第一个更新周期（下周一）
   - 检查是否自动创建 PR

4. **配置团队通知**（可选）:
   - Settings → Notifications → Dependabot alerts
   - 添加团队成员到审查列表

## 📚 参考文档

- [Dependabot 官方文档](https://docs.github.com/en/code-security/dependabot)
- [配置选项参考](https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file)
- [自动合并 PR](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/automating-dependabot-with-github-actions)

---

**最后更新**: 2026-01-08
**维护者**: Telegram Guard Bot Team
