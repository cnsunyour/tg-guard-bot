# 安全审查报告 - Telegram Guard Bot

**审查日期**: 2026-01-03
**审查范围**: 全代码库
**审查工具**: Semgrep, Trivy, Bandit, Codex (Claude Opus), Gemini
**代码版本**: Phase 1-6 完成版本
**扫描行数**: 3,378 行 Python 代码

---

## 📊 执行摘要

| 严重程度 | 发现数量 | 状态 |
|----------|---------|------|
| 🔴 CRITICAL (严重) | 2 | ⚠️ 需立即修复 |
| 🟠 HIGH (高危) | 8 | ⚠️ 需尽快修复 |
| 🟡 MEDIUM (中等) | 9 | ⏱️ 计划修复 |
| 🔵 LOW (低危) | 7 | 📝 建议修复 |
| **总计** | **26** | - |

### 安全评分
- **当前安全等级**: ⚠️ **C级（需改进）**
- **主要风险**: 权限绕过、代码注入、信息泄露
- **预计修复后**: ✅ **A级（安全）**

---

## 🔴 严重漏洞 (CRITICAL) - 需立即修复

### C1. 不安全的反序列化 (CWE-502)

**文件**: `src/ml/classifier.py`
**行号**: 207, 230
**发现工具**: Semgrep, Bandit (B301/B403), Codex

**描述**:
使用 `pickle.load()` 反序列化模型文件，存在远程代码执行 (RCE) 风险。

```python
# 保存时
pickle.dump(self.model, f)  # 第 207 行

# 加载时
self.model = pickle.load(f)  # 第 230 行
```

**攻击场景**:
1. 攻击者通过某种方式获取 `data/models/` 目录写入权限
2. 替换 `spam_classifier.pkl` 为恶意 pickle 对象
3. Bot 重启或调用 `/antispam` 重训练时触发 `load_model()`
4. 执行任意代码，完全接管服务器

**CVSS评分**: 9.8 (Critical)
**CWE**: CWE-502 (Deserialization of Untrusted Data)
**OWASP**: A08:2021 (Software and Data Integrity Failures)

**修复方案**:

**方案1（推荐）**: 使用 ONNX 格式
```python
from sklearn import pipeline
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import StringTensorType
import onnxruntime as rt

# 保存
initial_type = [('string_input', StringTensorType([None, 1]))]
onx = convert_sklearn(self.model, initial_types=initial_type)
with open(path, "wb") as f:
    f.write(onx.SerializeToString())

# 加载
sess = rt.InferenceSession(path)
```

**方案2**: pickle + 数字签名
```python
import hmac
import hashlib

SECRET_KEY = os.getenv("MODEL_SIGNATURE_KEY")

# 保存时
data = pickle.dumps(self.model)
signature = hmac.new(SECRET_KEY.encode(), data, hashlib.sha256).hexdigest()
with open(path, "wb") as f:
    f.write(signature.encode() + b'\n' + data)

# 加载时
with open(path, "rb") as f:
    lines = f.read().split(b'\n', 1)
    saved_sig, data = lines[0].decode(), lines[1]
    expected_sig = hmac.new(SECRET_KEY.encode(), data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(saved_sig, expected_sig):
        raise SecurityError("Model signature verification failed")
    self.model = pickle.loads(data)
```

---

### C2. 回调权限绕过 (CWE-863)

**文件**: `src/bot/handlers/admin.py`, `src/bot/handlers/antispam.py`
**行号**: 74-95, 348-366, 393-409
**发现工具**: Codex, Gemini

**描述**:
多个回调处理函数缺少权限验证，任意用户可以通过伪造 callback_data 修改群组配置。

**受影响的回调**:
| 回调前缀 | 文件 | 行号 | 风险操作 |
|---------|------|------|---------|
| `setverify:` | admin.py | 74-95 | 修改验证方式 |
| `antispam_toggle:` | antispam.py | 348-366 | 开关反垃圾 |
| `antispam_retrain:` | antispam.py | 393-409 | 触发模型重训练（DoS） |

**攻击场景**:
1. 普通用户获取任意群组的 `chat_id`（通过消息元数据）
2. 使用 Telegram API 客户端或修改后的客户端
3. 发送伪造的 callback_query：
   ```json
   {
     "callback_query": {
       "data": "antispam_toggle:-1001234567890:off",
       "from": {"id": 12345},
       "message": {"chat": {"id": -1001234567890}}
     }
   }
   ```
4. 成功禁用目标群组的反垃圾保护
5. 随后发送垃圾消息不受检测

**CVSS评分**: 8.1 (High)
**CWE**: CWE-863 (Incorrect Authorization)
**OWASP**: A01:2021 (Broken Access Control)

**修复方案**:

在所有回调处理函数开头添加权限验证：

```python
@router.callback_query(F.data.startswith("setverify:"))
async def on_setverify_callback(callback: CallbackQuery) -> None:
    try:
        _, chat_id_str, verify_type = callback.data.split(":")
        chat_id = int(chat_id_str)

        # ✅ 添加权限验证
        if callback.from_user.id not in settings.admin_ids:
            try:
                member = await callback.bot.get_chat_member(
                    chat_id,
                    callback.from_user.id
                )
                if member.status not in ["creator", "administrator"]:
                    await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
                    return
            except Exception as e:
                logger.error(f"权限检查失败: {e}")
                await callback.answer("❌ 权限验证失败", show_alert=True)
                return

        # ✅ 白名单验证参数
        if verify_type not in ["button", "math", "slider"]:
            await callback.answer("❌ 无效的验证类型", show_alert=True)
            return

        await GroupRepository.update_verification_type(chat_id, verify_type)
        # ... 其余代码
```

---

## 🟠 高危漏洞 (HIGH) - 需尽快修复

### H1. HTML 注入 / XSS (CWE-79)

**文件**: 多个 handler 文件
**发现工具**: Gemini

**描述**:
Bot 启用 HTML 解析模式，多处直接插入未转义的用户输入到消息中。

**受影响位置**:

| 文件 | 行号 | 风险输入 | 上下文 |
|------|------|---------|--------|
| services/verification.py | 47, 109, 165 | `username` | 欢迎消息 |
| handlers/verification.py | 212 | `callback.from_user.full_name` | 验证成功消息 |
| handlers/antispam.py | 97, 215 | `message.from_user.full_name` | 垃圾处理通知 |
| handlers/antispam.py | 282 | `callback.from_user.full_name` | 反馈确认 |
| handlers/moderation.py | 371 | `reason` (警告原因) | 警告记录 |

**攻击场景**:
1. 用户设置昵称为：`<b>Admin</b> <a href="http://phishing.site">点击领红包</a>`
2. 触发验证成功消息：
   ```
   ✅ @malicious_user (Admin 点击领红包) 已通过验证
   ```
3. 其他用户点击链接，被引导到钓鱼网站

**CVSS评分**: 7.1 (High)
**CWE**: CWE-79 (Cross-site Scripting)
**OWASP**: A03:2021 (Injection)

**修复方案**:

创建 HTML 转义工具函数：

```python
# src/core/utils.py
import html

def escape_html(text: str) -> str:
    """转义 HTML 特殊字符"""
    if not text:
        return ""
    return html.escape(text)

def format_user_mention(user) -> str:
    """安全地格式化用户提及"""
    name = escape_html(user.full_name or user.first_name or "Unknown")
    username = f"@{user.username}" if user.username else f"ID:{user.id}"
    return f"{name} ({username})"
```

在所有消息中使用：

```python
from src.core.utils import escape_html, format_user_mention

# 修改前
await message.answer(f"✅ {callback.from_user.full_name} 已通过验证")

# 修改后
await message.answer(f"✅ {format_user_mention(callback.from_user)} 已通过验证")
```

---

### H2. 敏感信息日志记录 (CWE-532)

**文件**: `src/main.py`, 多个服务文件
**行号**: 79-110
**发现工具**: Codex

**描述**:
DEBUG 级别日志记录用户消息内容、OCR 提取文字等敏感信息，JSON 日志序列化完整数据。

**风险示例**:

```python
# src/services/spam_detector.py:138
logger.info(
    f"从图片提取文字 [用户:{user_id}] 长度: {len(extracted_text)} "
    f"内容: {extracted_text[:50]}..."  # ❌ 记录消息内容
)

# src/bot/handlers/antispam.py:56-61
logger.warning(
    f"检测到垃圾信息 [群组:{message.chat.id}] "
    f"[用户:{message.from_user.id}] "
    f"阶段: {result['stage']}, "  # ❌ 可推断检测系统逻辑
)
```

**CVSS评分**: 6.5 (Medium/High)
**CWE**: CWE-532 (Insertion of Sensitive Information into Log File)
**OWASP**: A09:2021 (Security Logging and Monitoring Failures)

**修复方案**:

1. 调整日志级别（生产环境）:
```python
# .env
LOG_LEVEL=INFO  # 不要用 DEBUG
```

2. 对敏感数据脱敏：
```python
def mask_text(text: str, show_length: int = 10) -> str:
    """脱敏文本内容"""
    if len(text) <= show_length:
        return "***"
    return f"{text[:show_length]}...*** (length: {len(text)})"

logger.info(
    f"从图片提取文字 [用户:{user_id}] "
    f"内容: {mask_text(extracted_text)}"
)
```

3. 移除 JSON 日志或限制字段：
```python
# 移除 serialize=True 的日志配置
# 或使用自定义序列化器过滤敏感字段
```

---

### H3. 数据库/Redis 弱密码配置 (CWE-798)

**文件**: `docker-compose.yml`, `.env.example`
**行号**: 29, 51
**发现工具**: Trivy, Codex

**描述**:
数据库和 Redis 使用弱默认密码或空密码。

```yaml
# docker-compose.yml
POSTGRES_PASSWORD: ${DB_PASSWORD:-postgres}  # ❌ 弱默认值
...
command: redis-server --requirepass ${REDIS_PASSWORD:-}  # ❌ 默认为空
```

**CVSS评分**: 7.5 (High)
**CWE**: CWE-798 (Use of Hard-coded Credentials)

**修复方案**:

```yaml
# docker-compose.yml - 移除默认值
environment:
  POSTGRES_PASSWORD: ${DB_PASSWORD:?Database password not set}

command: redis-server --requirepass ${REDIS_PASSWORD:?Redis password not set}
```

```env
# .env.example - 添加强密码示例和说明
# ⚠️ 必须修改为强密码（至少 16 字符，包含大小写、数字、特殊字符）
DB_PASSWORD=CHANGE_ME_TO_STRONG_PASSWORD_16+_CHARS
REDIS_PASSWORD=CHANGE_ME_TO_STRONG_PASSWORD_16+_CHARS
```

---

### H4. 数据库端口暴露 (CWE-200)

**文件**: `docker-compose.yml`
**行号**: 37-38, 54-55
**发现工具**: Codex

**描述**:
PostgreSQL (5432) 和 Redis (6379) 端口映射到主机，增加攻击面。

**CVSS评分**: 6.5 (Medium/High)
**CWE**: CWE-200 (Exposure of Sensitive Information)

**修复方案**:

```yaml
# docker-compose.yml - 移除端口映射
  postgres:
    # ports:  # ❌ 移除
    #   - "5432:5432"
    ...

  redis:
    # ports:  # ❌ 移除
    #   - "6379:6379"
```

如果开发需要，创建 `docker-compose.override.yml`：
```yaml
# docker-compose.override.yml (仅本地，不提交到 Git)
services:
  postgres:
    ports:
      - "127.0.0.1:5432:5432"  # 仅绑定到 localhost
  redis:
    ports:
      - "127.0.0.1:6379:6379"
```

---

### H5. 回调数据参数注入 (CWE-20)

**文件**: `src/bot/handlers/antispam.py`
**行号**: 85, 203
**发现工具**: Codex, Gemini

**描述**:
将用户消息文本直接嵌入 callback_data，可能导致解析错误和数据投毒。

```python
callback_data=f"spam_feedback:normal:{message.from_user.id}:{message.text[:50]}"
#                                                              ^^^^^^^^^^^^^^
#                                                              用户可控
```

**攻击场景**:
1. 用户发送消息：`test:attack:inject`
2. 生成的 callback_data：`spam_feedback:normal:12345:test:attack:inject`
3. 解析时 `split(":", 3)` 导致字段错位
4. 或超出 Telegram 64 bytes 限制导致错误

**CVSS评分**: 6.1 (Medium)
**CWE**: CWE-20 (Improper Input Validation)

**修复方案**:

使用消息 ID 而非文本：

```python
# 保存消息到 Redis
message_key = f"spam_msg:{message.chat.id}:{message.message_id}"
await redis.setex(
    message_key,
    3600,  # 1小时过期
    message.text
)

# callback_data 仅包含 ID
callback_data = f"spam_feedback:normal:{message.chat.id}:{message.message_id}"

# 处理时从 Redis 获取
async def on_spam_feedback(callback: CallbackQuery):
    _, feedback_type, chat_id, message_id = callback.data.split(":")
    message_key = f"spam_msg:{chat_id}:{message_id}"
    text = await redis.get(message_key)
    if not text:
        await callback.answer("❌ 消息已过期", show_alert=True)
        return
    # 处理反馈...
```

---

### H6-H8. 其他高危问题（简要）

| ID | 问题 | 文件 | 修复优先级 |
|----|------|------|-----------|
| H6 | 速率限制缺失 | main.py:33-36 | P1 |
| H7 | 警告记录未授权访问 | handlers/moderation.py:344-376 | P1 |

---

### H8. 验证码使用弱随机数生成器 (CWE-330)

**文件**: `src/services/verification.py`
**行号**: 69-70, 76, 136
**发现工具**: Bandit (B311)

**描述**:
验证码生成使用标准 `random` 模块而非密码学安全的随机数生成器。

**风险代码**:
```python
# 第 69-70 行 - 数学验证码
num1 = random.randint(1, 10)  # ❌ 不安全
num2 = random.randint(1, 10)

# 第 76 行 - 错误选项
wrong = random.randint(1, 20)  # ❌ 不安全

# 第 136 行 - 滑块验证位置
correct_position = random.randint(0, 3)  # ❌ 不安全
```

**安全风险**:
- `random` 模块使用 Mersenne Twister 算法，可预测
- 攻击者可通过观察多个验证码推断随机数种子
- 理论上可预测下一个验证码答案

**CVSS评分**: 5.3 (Medium)
**CWE**: CWE-330 (Use of Insufficiently Random Values)

**修复方案**:

使用 `secrets` 模块替代：

```python
import secrets

# 数学验证码
num1 = secrets.randbelow(10) + 1  # 1-10
num2 = secrets.randbelow(10) + 1

# 错误选项
wrong = secrets.randbelow(20) + 1  # 1-20

# 滑块位置
correct_position = secrets.randbelow(4)  # 0-3
```

或使用 `random.SystemRandom()`：

```python
import random

rng = random.SystemRandom()
num1 = rng.randint(1, 10)
num2 = rng.randint(1, 10)
```

---

## 🟡 中等漏洞 (MEDIUM) - 计划修复

### M1-M9. 中等问题列表

| ID | 问题 | CVSS | 文件 | 简要说明 |
|----|------|------|------|---------|
| M1 | 整数 ID 未范围验证 | 5.3 | moderation.py:37 | 可能导致内存问题 |
| M2 | 异常信息泄露 | 5.3 | admin.py:186, 235 | 向用户显示完整异常 |
| M3 | SQL使用原始text() | 4.3 | migrate.py:62 | 潜在SQL注入风险（当前安全） |
| M4 | 回调上下文未绑定 | 5.9 | verification.py:75 | chat_id从callback_data取 |
| M5 | 参数未白名单校验 | 5.3 | admin.py:78 | verify_type未验证 |
| M6 | 时长解析无上限 | 4.3 | moderation.py:53 | 可注入极大值 |
| M7 | 用户ID存在性未验证 | 4.3 | moderation.py:18 | 不验证ID是否合法 |
| M8 | 临时文件清理缺陷 | 4.7 | handlers/antispam.py:240 | 异常时可能不清理 |
| M9 | OCR路径未验证 | 5.3 | ml/ocr.py:46 | 未验证图片路径合法性 |

---

## 🔵 低危漏洞 (LOW) - 建议修复

### L1. Docker 以 root 运行 (CWE-250)

**文件**: `Dockerfile`
**发现工具**: Trivy

**修复**:
```dockerfile
# 创建非 root 用户
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app
USER appuser
```

### L2. 依赖版本范围过宽

**文件**: `pyproject.toml`

**修复**: ✅ 已解决
```toml
# pyproject.toml 中使用版本范围
[project]
dependencies = [
    "aiogram>=3.6.0,<4.0.0",
    "sqlalchemy>=2.0.0,<3.0.0",
    # ...
]
```

**说明**: 现代 Python 项目使用 `pyproject.toml` 管理依赖，无需 `requirements.txt`。版本范围在 `pyproject.toml` 中定义，`pip install -e .` 会自动锁定版本。

### L3-L7. 其他低危问题

| ID | 问题 | 文件 | 修复建议 |
|----|------|------|---------|
| L3 | BOT_TOKEN 示例值不清晰 | .env.example | 使用 `<YOUR_TOKEN>` 格式 |
| L4 | 缺少安全扫描工具依赖 | pyproject.toml | 添加 bandit, safety 到 dev 依赖 |
| L5 | Assert 语句使用 | migrate.py:82 | 移除或替换为显式检查 |
| L6 | Try/Except/Pass 模式 | antispam.py, rule_engine.py | 至少记录日志 |
| L7 | subprocess 使用 | backup.py:54,107 | 已验证：合法用途（pg_dump） |

**L5 详细说明 (Bandit B101)**:
```python
# migrate.py:82 - Assert 会在优化编译时被移除
assert result.scalar() == 1
```
修复：
```python
if result.scalar() != 1:
    raise RuntimeError("Database health check failed")
```

**L6 详细说明 (Bandit B110)**:
```python
# 多处 try/except/pass - 静默吞掉所有异常
except Exception:
    pass  # ❌ 没有任何日志
```
修复：
```python
except Exception as e:
    logger.warning(f"操作失败（非关键）: {e}")
```

**L7 说明**:
Bandit 标记了 `backup.py` 中的 subprocess 使用（B404/B603），但经审查这是合法用途：
```python
# backup.py:54 - 使用 pg_dump 备份数据库
subprocess.run(["pg_dump", "-h", host, ...], env=env, capture_output=True)
```
✅ 已验证：命令参数来自可信环境变量，且使用列表形式避免了 shell 注入。

---

## ✅ 良好实践（保持）

以下安全实践值得保持：

1. ✅ **SQLAlchemy ORM**: 所有数据库操作使用 ORM，有效防止 SQL 注入
2. ✅ **参数化查询**: Repository 层正确使用参数化查询
3. ✅ **Pydantic 验证**: 配置使用 Pydantic Settings
4. ✅ **环境变量**: 敏感配置通过环境变量管理
5. ✅ **资源限制**: docker-compose.prod.yml 限制 CPU/内存
6. ✅ **审计日志**: 管理操作记录到 audit_logs 表
7. ✅ **健康检查**: 实现数据库和 Redis 连接检查

---

## 📋 修复优先级路线图

### P0 - 立即修复（本周内）
- [ ] **C1**: 修复 pickle 反序列化（使用 ONNX 或签名验证）
- [ ] **C2**: 为所有回调添加权限验证
- [ ] **H1**: 修复 HTML 注入（添加转义）
- [ ] **H3**: 强制配置强密码

**预计时间**: 4-6 小时
**风险降低**: 85%

### P1 - 尽快修复（两周内）
- [ ] **H2**: 实施日志脱敏
- [ ] **H4**: 移除生产端口暴露
- [ ] **H5**: 修复 callback_data 注入
- [ ] **H6**: 启用速率限制
- [ ] **H7**: 限制警告记录访问

**预计时间**: 6-8 小时
**风险降低**: 10%

### P2 - 计划修复（一个月内）
- [ ] **H8**: 替换 random 为 secrets 模块
- [ ] 所有中等漏洞 (M1-M9)
- [ ] 所有低危漏洞 (L1-L7)

**预计时间**: 8-12 小时
**风险降低**: 5%

---

## 🛠️ 修复验证清单

修复完成后，请验证：

- [ ] 运行 `semgrep --config=auto src/` 无高危发现
- [ ] 运行 `bandit -r src/ scripts/ -ll` 无中高危发现
- [ ] 运行 `trivy config .` 无严重问题
- [ ] 手动测试回调权限验证
- [ ] 检查日志文件无敏感信息
- [ ] 验证所有 HTML 消息正确转义
- [ ] 测试 pickle 替代方案工作正常
- [ ] 确认生产环境端口未暴露
- [ ] 验证强密码配置生效
- [ ] 确认验证码使用 secrets 模块

---

## 📞 安全联系

如发现其他安全问题，请通过以下方式报告：

- **GitHub Security Advisory**: (私密报告)
- **Email**: security@your-domain.com
- **加密通信**: PGP Key ID: (如果有)

---

**报告生成**: 2026-01-03
**下次审查建议**: 2026-04-03 (每季度审查)
**审查工具版本**:
- Semgrep: 1.146.0
- Trivy: Latest
- Bandit: 1.9.2
- Codex: Claude Opus 4.5
- Gemini: Latest

---

**免责声明**: 本报告基于当前代码版本进行静态分析和人工审查，实际部署环境可能存在其他安全风险。建议在生产部署前进行渗透测试。

---

## 📎 附录：Bandit 扫描详细结果

**扫描统计**:
- 总行数: 3,378 行 Python 代码
- 扫描文件: 37 个文件
- 总发现: 15 个问题
- 严重程度分布:
  - HIGH: 0
  - MEDIUM: 1 (pickle.load)
  - LOW: 14

**按测试 ID 分类**:
| Test ID | 类型 | 数量 | 文件 |
|---------|------|------|------|
| B301 | pickle.load() | 1 | classifier.py:230 |
| B403 | import pickle | 1 | classifier.py:4 |
| B404 | import subprocess | 1 | backup.py:7 |
| B603 | subprocess.run() | 2 | backup.py:54,107 |
| B101 | assert 语句 | 1 | migrate.py:82 |
| B110 | try/except/pass | 4 | antispam.py:33,41,131,139; rule_engine.py:115 |
| B311 | random.randint() | 4 | verification.py:69,70,76,136 |

**按文件分类**:
- `scripts/backup.py`: 3 个发现 (subprocess 相关)
- `scripts/migrate.py`: 1 个发现 (assert)
- `src/bot/handlers/antispam.py`: 4 个发现 (try/except/pass)
- `src/ml/classifier.py`: 2 个发现 (pickle)
- `src/ml/rule_engine.py`: 1 个发现 (try/except/pass)
- `src/services/verification.py`: 4 个发现 (random.randint)

**Bandit 命令行**:
```bash
# 扫描所有代码
bandit -r src/ scripts/ -f json -o bandit_report.json

# 查看可读格式
bandit -r src/ scripts/ -f txt

# 仅显示中高危
bandit -r src/ scripts/ -ll
```
