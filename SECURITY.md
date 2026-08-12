# 安全策略 (Security Policy)

## 🔒 支持的版本

当前仅维护最新版本的安全更新。低于下表所列版本的部署请升级至最新 release。

| 版本 | 支持状态 |
| --- | --- |
| 1.8.1 | ✅ 当前支持 |
| ≤ 1.8.0 | ❌ 不再支持，请升级至最新版本 |

> **说明**：v1.8.0 及更早版本存在已修复的安全问题（如 v1.8.1 收紧的入群限制权限、v1.7.2 全项目审查加固的 12 项修复），不再提供补丁，请直接升级到最新版本。

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
- ✅ 高风险操作使用 Telegram API 实时 **strict 权限校验**（非缓存）
- ✅ `/cleanup` 批量踢人 TOCTOU 防护：扫描后、执行子命令前再次校验操作者权限，防止扫描缓存 TTL 期间权限被撤销仍批量操作
- ✅ Callback 权限校验使用点击者 `from_user.id`（非 `message` owner），API 失败时 fail-closed，防授权绕过
- ✅ `/verifyconfig` 仅管理员可查看（缓存版权限校验）
- ✅ 白名单中间件覆盖 `message`、`callback_query` 与 `chat_join_request`（加入请求模式）；`/whitelist remove` 成功后自动退群
- ✅ 反垃圾确认按钮 `try/finally` 统一清理，防按钮残留被重复点击
- ✅ 所有敏感操作强制权限检查

### 输入验证
- ✅ 用户 ID 范围验证（防止整数溢出）
- ✅ 参数白名单验证
- ✅ 禁言时长严格解析（`parse_time_to_seconds` 严格 `fullmatch`，拒绝 `30mxxx`/`0m` 等畸形输入，上限 366 天）
- ✅ 反垃圾 feedback callback 精确 4 段解析 + `feedback_type∈{normal,spam}` 白名单 + 正整数 ID
- ✅ 自定义正则规则配置上限：文件 ≤1MB / 规则 ≤200 / pattern ≤500 字符，编译失败或重复 ID 自动剔除（防 ReDoS 与大文件滥用）
- ✅ CAPTCHA 配置依赖校验：配置 `CAPTCHA_WEBAPP_URL` 时 `CAPTCHA_SIGNATURE_KEY` 须 ≥32 字符（`webapp_url` 是所有 provider 回调的先决条件，含 `/setverify turnstile` 显式选择路径）

### 数据保护
- ✅ 敏感信息日志脱敏
- ✅ HTML 注入防护（所有用户可控文本经 `escape_html` 转义）
- ✅ proxy URL 日志脱敏（`_redact_proxy_url` 统一剥离 userinfo，覆盖无协议 `user@host:port` 与 `urlparse` 误识 `user:` 为 scheme 的边界）
- ✅ Sentry `before_send` 事件清洗（递归 depth 20 清洗整个 event + 字段名归一化过滤，覆盖 `X-API-Key` / `private-key` 等含连字符 HTTP header）
- ✅ MTCaptcha 服务端验证函数不再主动记录 token（GET 契约保留，见[已知限制](#-已知限制与已接受风险)）
- ✅ 群组消息普通用户名称脱敏（`format_user_mention` / `masked_mention_html`，先脱敏后 HTML 转义），管理员/操作者名称完整显示
- ✅ httpx 异常 safe 模式提取响应体白名单字段并脱敏 URL
- ✅ 生产环境拒绝默认数据库密码（`postgres`）与空 Redis 密码（建议 ≥16 位强密码，见[部署清单](#-安全检查清单)）

### 加密与随机数
- ✅ 使用 `secrets` 模块生成验证挑战、session token、in-flight owner token
- ✅ ML 模型文件 HMAC-SHA256 签名验证 + 常量时间比较（`compare_digest`），密钥强制 ≥32 字符
- ✅ WebApp 回调绑定 `chat/user/token/timestamp`，验证 HMAC 签名 + 时效 + 一次性 Redis token
- ✅ Redis / PostgreSQL 密码认证（注：本项指客户端认证配置，不涉及传输层或静态加密）

### WebApp / CAPTCHA 安全
- ✅ CAPTCHA provider 白名单（friendly/hcaptcha/mtcaptcha/altcha/turnstile）
- ✅ WebApp 回调 HMAC 签名 + 时间戳时效验证 + 一次性 Redis token（防重放）
- ✅ 签名密钥条件校验（启用 WebApp 时强制 ≥32 字符）
- ✅ `/setverify turnstile` 显式选择不绕过 `webapp_captcha_enabled` 前提
- ✅ 生产环境禁止加载未签名 ML 模型
- ✅ 第三方 CAPTCHA 服务（Friendly/hCaptcha/MTCaptcha/Turnstile）信任边界限定在其官方 verify API

### 反滥用与并发安全
- ✅ CAS（Combot Anti-Spam）黑名单集成，与本地检测形成双重防护
- ✅ 入群短窗口消息防护中间件：删除新成员在 `restrict_chat_member` 生效前抢发的群消息
- ✅ 入群处理三层去重：事件去抖（60s）+ in-flight owner-token 互斥锁（`SET NX EX` + Lua compare-and-delete 释放）+ pending 验证去重
- ✅ Callback 速率限制（5 次/秒，防按钮刷点）
- ✅ 反垃圾确认面板互斥（同一消息不重复弹确认）
- ✅ v1.8.1 收紧未验证成员权限：显式禁用 react / 编辑群头衔 / 发起话题 / 链接预览等全部发言相关权限

### DoS 防护
- ✅ **Callback 速率限制 5 次/秒**；消息不做通用速率限制，统一交由反垃圾系统处理（检测为垃圾后直接封禁/禁言，比简单限流更有效）
- ✅ 禁言/限制时长上限（366 天）
- ✅ 自定义规则配置规模限制（文件/规则数/pattern 长度，见输入验证）
- ✅ 模型文件大小限制（100MB）
- ✅ Vision 图片大小、AI 检测超时与重试次数上限
- ✅ 上下文消息缓存 TTL 自动清理（Redis）
- ✅ 入群 in-flight 锁 TTL 兜底（进程异常退出防死锁）

### 运行时安全
- ✅ Docker 容器非 root 用户运行（`user: 1000:1000`）
- ✅ 数据库 / Redis 端口默认不对外暴露
- ✅ 最小权限原则（`ChatPermissions` 仅授予必要字段）
- ✅ Telethon session 文件安全（`lstat` + 拒绝符号链接 + group/others 可读权限告警）
- ✅ ML 模型加载安全（拒绝符号链接 / 非普通文件 / 超大文件 / 无效签名）
- ✅ v1.8.1 未验证成员 `ChatPermissions` 显式禁用全部发言相关权限
- ✅ UTC-aware 的 Telegram 时限计算（`until_date` 等在所有时区一致，消除 naive datetime 偏差）
- ✅ Vision 图片处理临时文件 `finally` 自动清理

### 审计与可观测
- ✅ 操作审计日志（`audit_logs` 表）覆盖群管理、反垃圾、验证、CAS 等操作
- ⚠️ 日志分级（建议生产 `LOG_LEVEL=INFO`+；当前配置未在 `DEBUG=false` 时强制拒绝 `LOG_LEVEL=DEBUG`，生产部署须显式设置该变量）
- ✅ Sentry 错误监控与事件清洗（见数据保护）
- ⚠️ 注：当前代码未提供日志不可篡改或固定保留期的保证，日志轮转由文件系统按日期切分

### 依赖安全
- ✅ `pyproject.toml` 统一管理依赖边界与已知安全版本下限
- ✅ 关键 CVE 修复约束：`aiohttp>=3.13.3`（CVE-2025-69223~69230）、`urllib3>=2.6.3`（CVE-2026-21441）、`filelock>=3.20.3`（CVE-2026-22701）、`pyasn1>=0.6.2`（CVE-2026-23490）
- ✅ 每周 CI 安全扫描（`.github/workflows/security.yml`）：Bandit、Safety、pip-audit、Semgrep、Trivy（镜像）、Gitleaks（密钥）
- ✅ PR 依赖审查（`dependency-review-action`，`fail-on-severity: moderate`，拒绝 GPL-3.0/AGPL-3.0）
- ⚠️ 注：Bandit/Safety/pip-audit/Semgrep/Trivy 当前为 `continue-on-error`（监测告警，非强制门禁）；Gitleaks 与 dependency-review 为门禁

---

## ⚠️ 已知限制与已接受风险

经评估，以下风险已通过限制攻击面或依赖官方契约，作为已知接受项。每项标注影响范围、已有缓解、残余场景与复审触发条件。

### L6 自定义规则 ReDoS 残余
- **影响**：自定义正则规则引擎（`src/ml/rule_engine.py`）
- **缓解**：规则文件 ≤1MB / 规则数 ≤200 / pattern ≤500 字符上限；编译失败自动剔除；输入文本默认截断
- **残余**：Python `re` 模块无单次匹配超时，可信管理员仍可配置灾难性回溯表达式
- **接受理由**：规则仅由群管理员本地配置，非远程用户可控；攻击面已被规模限制大幅收窄
- **复审触发**：若未来支持远程编辑/导入规则，或出现匹配延迟告警，应改用 RE2、带 timeout 的引擎或隔离进程执行

### M5 MTCaptcha URL query 残余
- **影响**：MTCaptcha 服务端验证（GET 请求契约）
- **缓解**：服务端验证函数已删除主动 token 日志输出
- **残余**：MTCaptcha 官方仅提供 GET verify 契约，private key 与 token 进入 URL query；HTTPS 降低链路窃听，但 vendor、TLS 终止代理、URL 日志与遥测仍可能看到。此外 WebApp 前端 `mtcaptcha.html` 残留调试用 `console.log('MTCaptcha verified:', token)`（一次性 token + 仅用户本地浏览器可见，低风险）
- **接受理由**：受限于第三方官方契约，无 POST 替代；token 一次性使用后失效
- **复审触发**：MTCaptcha 官方提供 POST 契约时切换；前端 console.log 待后续 WebApp 清理一并移除

### 部分 CI 扫描为监测非门禁
- **影响**：Bandit/Safety/pip-audit/Semgrep/Trivy 当前 `continue-on-error`
- **缓解**：Bandit/Safety/pip-audit/Trivy 报告作为 artifact 上传，Trivy SARIF 上传至 GitHub Security；Gitleaks 与 dependency-review 为强制门禁（Semgrep 结果仅 CI 内可见，不上传 artifact）
- **接受理由**：避免第三方扫描的误报阻断正常迭代，同时保持可见性
- **复审触发**：扫描结果稳定后可逐步收紧为门禁

---

## 🔍 安全审计记录

### 2026-08-12 - v1.8.1 安全相关修复
- **入群限制权限收紧**：`ChatPermissions` 显式禁用 react / 编辑群头衔 / 发起话题 / 链接预览等全部发言相关权限，堵住未验证用户绕过门槛表态/改头衔/起话题的漏洞
- **datetime 时区脆弱性消除**：双函数架构（`utcnow` aware / `utcnow_naive`），禁言/限制 `until_date` 在非 UTC 服务器不再偏差 8 小时
- **状态**：已修复并发布

### 2026-08-08 - v1.7.2 全项目安全审查
- **范围**：Bot 主体、WebApp/CAPTCHA、部署配置、日志与凭证、依赖及历史提交
- **方法与验证**：全项目代码审查 + Codex 边界复核 + Gitleaks（537 commits）+ Trivy
- **发现**：12 项问题（Top5 中高危 + P1×2 边界 + Low×7），**0 Critical / 0 High**
- **修复**：12/12 全部完成
- **验证结果**：Gitleaks 扫描 537 commits 零泄露；Trivy 零漏洞
- **已接受风险**：L6 ReDoS 残余、M5 MTCaptcha URL query，详见[已知限制](#-已知限制与已接受风险)
- **状态**：无未处置 Critical/High；两项残余风险已记录并接受

#### 修复详情（按主题分组）
- **授权与 TOCTOU**：`/cleanup` 批量踢人 strict 重检（M2）、白名单覆盖 `chat_join_request` + 退群（M1）、`/verifyconfig` 权限校验（L1）
- **凭证与日志**：proxy URL 统一脱敏含无协议形式（M3）、Sentry `before_send` 深度清洗（L5）、Redis 明文密码消除 `REDISCLI_AUTH`（L7）、MTCaptcha token 日志删除（M5）
- **CAPTCHA**：签名密钥条件校验（M4）、`/setverify turnstile` 绕过 `webapp_captcha_enabled` 前提（P1）、proxy 无协议泄露（P1）
- **输入验证**：禁言时长严格 `fullmatch`（L3）、反垃圾 feedback 精确解析（L4）
- **文件安全**：Telethon session `lstat` + 拒绝符号链接 + 权限告警（L2）
- **资源限制**：自定义规则规模上限防 ReDoS（L6）

### 2026-08-07 - v1.7.1 安全相关修复
- **on_\* 处理器前置统一**：抽取 `_run_message_prechecks` 公共函数，修复 `on_photo` 频道路径遗漏（频道图片可绕过反垃圾）
- **命令检查前移**：移到频道过滤之后，堵住 anti-channel 绕过
- **新增** `tests/test_antispam_prechecks.py`（34 个测试）锁定前置过滤行为

### 2026-07-24 - v1.6.4 安全相关修复
- **举报按钮 callback 授权绕过修复**：权限校验改用点击者 `from_user.id`，拒绝后不进入业务处理，API 失败 fail-closed
- **管理员/操作者名称不脱敏**：新增 `format_trusted_user_mention()`，修复欢迎消息/反垃圾提示中管理员名称误用脱敏函数的问题
- **新增** `tests/test_moderation_callbacks.py` 回归测试

### 2026-07-17 - v1.6.0 安全相关修复
- **入群短窗口消息防护中间件**：删除新成员在 `restrict_chat_member` 生效前抢发的群消息
- **群组消息用户名称脱敏**：`format_user_mention` / `masked_mention_html` 统一应用（欢迎消息、CAS 通知等），管理员名称不脱敏
- **CAS 通知改进**：群组通知显示脱敏用户名（原仅数字 ID），移除违规次数显示
- **httpx 异常格式化**：`src/core/http_errors.py` safe 模式 URL 脱敏
- **网络错误重试**：`retry_async_call` 对权限恢复与加入请求批准做重试（指数退避）

### 2026-02-12 - v1.2.0 安全更新
- **更新内容**：模型签名验证增强
- **新增**：
  - 模型签名密钥强制配置（`MODEL_SIGNATURE_KEY`）
  - 生产环境禁止加载未签名模型
  - 模型文件大小限制（100MB）
  - 模型文件权限检查
  - 符号链接拒绝加载
- **状态**：所有安全措施已实施

### 2026-01-03 - 全面安全审计
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
- [ ] `CAPTCHA_SIGNATURE_KEY` 使用随机生成的 ≥32 字符密钥（启用 WebApp 时）

#### 网络安全
- [ ] 数据库端口未对外暴露（docker-compose.yml 已移除端口映射）
- [ ] Redis 端口未对外暴露
- [ ] 使用防火墙限制入站连接
- [ ] 考虑使用反向代理（Nginx）

#### 容器安全
- [ ] 容器以非 root 用户运行（`user: 1000:1000`）
- [ ] Volume 权限正确设置
- [ ] 日志轮转配置正确
- [ ] 评估是否启用只读根文件系统（`read_only: true` 为可选加固，非默认配置；启用前确认无写入需求或已配 `tmpfs`）

#### 应用安全
- [ ] Callback 速率限制已启用（默认 5 次/秒）
- [ ] 日志级别设为 INFO 或以上（生产环境避免 DEBUG）
- [ ] 所有管理员命令需要权限验证
- [ ] 反垃圾系统已启用（替代消息级简单限流）

### 运行时监控
- [ ] 定期检查日志中的安全警告
- [ ] 监控异常 callback 速率限制触发
- [ ] 追踪 `audit_logs` 审计日志
- [ ] 检查数据库连接数和性能
- [ ] 关注 CI 安全扫描报告（GitHub Actions artifacts / Security tab）

### 定期维护
- [ ] 每月运行 `safety scan` 检查依赖漏洞
- [ ] 每月运行 `pip-audit` 检查依赖漏洞
- [ ] 每月运行 `bandit -r src/` 扫描代码
- [ ] 每季度运行 `gitleaks` 扫描历史提交泄露
- [ ] 每季度做一次安全复审（检查已知限制与残余风险是否仍可接受）
- [ ] 每年或重大架构变更后做全面安全审计

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

# CAPTCHA 签名密钥（启用 WebApp 验证时必填，≥32 字符）
CAPTCHA_SIGNATURE_KEY=$(openssl rand -hex 32)

# 生产环境日志
LOG_LEVEL=INFO
DEBUG=false
```

> **注**：消息/回调速率限制不通过环境变量配置（callback 固定 5 次/秒，消息不限流交反垃圾系统）。

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
    # 只读根文件系统（可选加固，非默认；启用前需配 tmpfs 处理临时写入）
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

**最后更新**：2026-08-12
**下次复审**：2026-11-08（季度安全复审）
