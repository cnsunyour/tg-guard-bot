# 更新日志

本项目的所有重要变更都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.5.1] - 2026-06-18

### Bug 修复

#### Telegram 消息 HTML 解析错误修复 🐛
- **HTML 实体转义**：修复所有 Telegram 消息中比较符号未转义导致的 HTML 解析错误
  - `/activity` 命令的活跃度说明（`<= 0` 等改为 HTML 实体 `&lt;= 0`）
  - `/activityskip` 命令的两处错误提示（`>0` 改为 `&gt;0`）
  - 活跃度限制通知消息（`> 0` 改为 `&gt;0`）
- **统一格式**：统一所有活跃度相关说明的表述，确保比较符号正确转义为 HTML 实体

#### `/activity` 命令显示修复 ⭐
- **规则描述与实际逻辑对齐**：修正 `/activity` 命令显示内容与实际规则不一致的问题
  - 非文本消息限制：明确 `<= 0` 时禁止发送
  - 活跃度变化：+1 文本消息，非文本消息不变（当前不扣分）
  - 衰减规则：仅当活跃度 < 10 且当天无消息时 -1
- **统一显示**：统一命令显示和回调显示的文本内容
- **回调状态修复**：回调函数中重新获取群组配置以确保显示最新状态
- **格式优化**：添加 emoji 图标和层次结构

### 重构优化

#### 移除温度参数配置 🔄
- **提高 API 兼容性**：删除 4 个温度配置字段，解决部分服务商（如 o1 系列）不允许传递 `temperature` 参数导致的调用失败（400/422 错误）
  - 删除 `ai_spam_temperature`
  - 删除 `ai_spam_backup_temperature`
  - 删除 `ai_spam_vision_temperature`
  - 删除 `ai_spam_vision_backup_temperature`
- **清理调用代码**：从文本检测 `_call_api()` 和 Vision 检测 `detect_image()` 中移除 `temperature` 参数
- **技术原因**：垃圾检测为确定性判断任务，所有温度值均为 0.0，不传参让服务商使用默认值即可

### 文档更新

- **README.md**：删除所有 OCR 功能说明，新增 AI Vision 多模态检测配置说明，更新技术栈表格、性能指标和开发路线图
- **CLAUDE.md**：更新代码结构（ocr.py → ai_detector.py）、更新资源要求，移除 Git 分支工作流章节
- **.env.example**：删除 4 个温度配置项

### 代码质量
- ✅ Config 加载正常
- ✅ AI Detector 加载正常
- ✅ Mypy 类型检查通过
- ✅ Ruff 代码检查通过

## [1.5.0] - 2026-06-16

### 删除功能

#### 彻底删除 OCR 功能 🗑️
- **删除所有 OCR 代码**：移除 1400+ 行 OCR 相关代码
  - 删除 `src/ml/ocr.py`、`src/ml/hybrid_ocr.py`、`src/ml/ocr_providers.py`
  - 删除 `scripts/test_easyocr.py`
  - 删除所有 OCR 配置环境变量（14 个）
- **删除 OCR 依赖包**：移除重型机器学习依赖
  - torch, torchvision, paddleocr, paddlepaddle, easyocr
  - openai, baidu-aip（OCR 相关）
  - 镜像体积减少 2-3GB
  - 内存需求降低：4GB → 2GB
- **删除 Makefile 命令**：移除 `prod-build-ocr` 构建命令

### 新增功能

#### Vision 多模态检测独立配置 ✨
- **主备双服务商架构**：Vision 检测支持主备自动回退
  - 新增 `VisionServiceProvider` 通用类
  - 支持 Vision 主服务商配置（6 个字段）
  - 支持 Vision 备服务商配置（7 个字段）
  - 对齐文本检测的主备架构
- **自动回退机制**：key/base 可留空回退文本配置
  - 新增 4 个 computed properties 实现自动回退
  - `vision_api_key_effective`：留空回退 `ai_spam_api_key`
  - `vision_api_base_effective`：留空回退 `ai_spam_api_base`
  - `vision_backup_api_key_effective`：留空回退 `ai_spam_backup_api_key`
  - `vision_backup_api_base_effective`：留空回退 `ai_spam_backup_api_base`
- **独立判断属性**：新增 `vision_enabled` 属性
  - 独立于 `ai_spam_enabled` 判断
  - 支持"只开文本"或"只开 Vision"场景

### 重构优化

#### Vision 检测架构重构 🔄
- **与文本检测解耦**：Vision 不再依赖 `AI_SPAM_ENABLED`
  - 文本和图片可使用不同模型（成本优化）
  - `AI_SPAM_MODEL` 用于文本检测
  - `AI_SPAM_VISION_MODEL` 用于图片/贴纸检测
- **HybridAIDetector 重构**：支持 vision_primary/vision_backup
  - Vision 主备服务商独立管理
  - 熔断器、统计追踪完全独立
  - 与文本检测共享 HTTP 客户端生命周期管理
- **图片检测行为变更**：移除 OCR 降级路径
  - 旧行为：Vision 失败 → 降级 OCR → 文本检测
  - 新行为：Vision 不可用/失败 → 跳过图片检测（放行）
  - 记录日志说明跳过原因

#### 字段重命名（向后兼容）🔤
- **统一命名规范**：去除 OCR 痕迹
  - `ocr_text` → `recognized_text`
  - `has_ocr` → `has_text`
  - `_extract_ocr_text_from_prompt` → `_extract_recognized_text_from_prompt`
  - 提示文案："OCR 识别内容" → "识别内容"
- **功能保留**：字段语义不变，仅命名更中性

#### 依赖优化 📦
- **lottie/cairosvg 移到主依赖**：TGS 动画贴纸渲染需要
  - 从 `[project.optional-dependencies].ocr` 移到 `[project.dependencies]`
  - 与 OCR 引擎无关，单独保留
- **删除 ocr 依赖组**：`[project.optional-dependencies].ocr` 整组删除
- **简化 all 组**：`tg-guard-bot[dev,ocr]` → `tg-guard-bot[dev]`

### 变更

#### 破坏性变更 ⚠️
- **图片检测默认关闭**：`AI_SPAM_VISION_ENABLED` 默认值改为 `false`
  - 更安全的默认配置
  - 需主动配置才能启用图片检测
- **无 OCR 兜底**：Vision 不可用时跳过图片检测
  - 删除 OCR 降级路径
  - 未配置 Vision 或 Vision 失败时，图片/贴纸直接放行
- **模型独立配置**：必须配置 `AI_SPAM_VISION_MODEL`
  - 需配置为多模态模型（如 gpt-4o-mini, claude-3-5-sonnet）
  - 不再从 `AI_SPAM_MODEL` 继承

#### 配置变更 🔧
- **新增 Vision 配置**：13 个新环境变量
  - `AI_SPAM_VISION_ENABLED`、`AI_SPAM_VISION_MODEL` 等（主服务商）
  - `AI_SPAM_VISION_BACKUP_ENABLED`、`AI_SPAM_VISION_BACKUP_MODEL` 等（备服务商）
  - 详见 `.env.example`
- **删除 OCR 配置**：14 个 OCR 环境变量全部删除
  - `ENABLE_OCR`、`OCR_OPENAI_*`、`OCR_BAIDU_*`、`OCR_PADDLE_*`、`OCR_EASY_*`
- **内存需求调整**：`docker-compose.prod.yml` 内存限制
  - 4GB → 2GB（嵌入模型 + ML 分类器）

### 优化

#### 成本优化 💰
- **模型分离策略**：文本和图片使用不同模型
  - 文本检测：便宜纯文本模型（如 deepseek-chat, $0.001/1K tokens）
  - 图片检测：多模态模型（如 gpt-4o-mini, $0.15/1K tokens）
  - **预计节省 90%+ API 成本**（纯文本消息占比 >95%）
- **示例计算**：日均 10000 条消息（9500 文本 + 500 图片）
  - 改造前：全用 gpt-4o-mini = $45/月
  - 改造后：文本用 deepseek-chat + 图片用 gpt-4o-mini = $2.55/月
  - **节省成本：94.3%** 🔥

#### 资源优化 🚀
- **镜像体积**：减少 2-3GB（无 torch/paddle/easyocr）
- **内存需求**：4GB → 2GB
- **启动时间**：更快（无需加载 OCR 模型）
- **部署成本**：更低（更小的服务器即可运行）

#### 高可用性 🛡️
- **Vision 主备回退**：与文本检测架构完全对齐
- **配置灵活性**：key/base 可留空回退
- **独立控制**：文本和图片检测独立开关

### 代码质量

- **代码减少**：净减少 1403 行代码（16 个文件修改）
- **测试覆盖**：✅ 所有单元测试通过（8 passed）
- **静态检查**：✅ Ruff 检查通过
- **类型检查**：✅ Mypy 检查通过
- **导入测试**：✅ Python 模块导入正常
- **残留检查**：✅ 无残留 OCR 引用

### 升级指南

#### 最简配置（复用文本 key/base）
```bash
# 文本检测：便宜纯文本模型
AI_SPAM_ENABLED=true
AI_SPAM_API_KEY=sk-xxx
AI_SPAM_API_BASE=https://api.openai.com/v1
AI_SPAM_MODEL=deepseek-chat

# 图片检测：多模态模型（key/base 留空自动回退上面的配置）
AI_SPAM_VISION_ENABLED=true
AI_SPAM_VISION_MODEL=gpt-4o-mini
```

#### 完整配置（主备双服务商）
```bash
# 文本主服务商
AI_SPAM_ENABLED=true
AI_SPAM_API_KEY=sk-main-xxx
AI_SPAM_MODEL=deepseek-chat

# 文本备服务商
AI_SPAM_BACKUP_ENABLED=true
AI_SPAM_BACKUP_API_KEY=sk-backup-xxx
AI_SPAM_BACKUP_MODEL=glm-4-flash

# Vision 主服务商（key/base 留空回退文本主）
AI_SPAM_VISION_ENABLED=true
AI_SPAM_VISION_MODEL=gpt-4o-mini

# Vision 备服务商（key/base 留空回退文本备）
AI_SPAM_VISION_BACKUP_ENABLED=true
AI_SPAM_VISION_BACKUP_MODEL=claude-3-5-sonnet
```

---

## [1.4.3] - 2026-06-15

### 重构优化

#### 活跃度系统简化 ⭐
- **移除全局开关**：删除 `settings.activity_enabled` 全局配置，简化控制逻辑
  - 只保留群组开关 `group.activity_enabled`
  - 群组开关只控制"是否限制活跃度 ≤ 0 的用户发送非文本消息"
  - 降低配置复杂度，提升易用性
- **辅助功能始终工作**：活跃度记录、置信度修正、检测豁免功能不受开关影响
  - 高活跃用户始终享受垃圾检测误判率降低的好处
  - 高活跃用户始终享受检测豁免（如果设置了阈值）
  - 宵禁模式下的活跃度门槛继续生效
- **向后兼容**：数据库结构无变化，群组配置完全兼容
  - 现有群组行为不变
  - 活跃度数据完整保留
  - 即使 `.env` 中保留 `ACTIVITY_ENABLED` 也不会报错

### Bug 修复
- **修复重复调用 bug**：修复 `/activity` 命令回调处理中重复调用数据库更新的问题

### 文档更新
- 更新 README.md - 活跃度系统说明
- 更新 CLAUDE.md - 配置和流程说明
- 更新 migrations/UPGRADE_GUIDE.md - 升级指南
- 更新 .env.example - 删除 ACTIVITY_ENABLED 配置项
- 更新 `/activity` 命令文案 - 更清晰的功能说明

### 代码质量
- ✅ 语法检查通过
- ✅ 代码格式化完成（Black + isort）
- ✅ Ruff lint 检查通过
- ✅ 所有单元测试通过

## [1.4.2] - 2026-06-14

### 新增功能

#### CAS 检测功能增强
- **Telethon 用户状态检测**：扩展 CAS 检测功能，增加基于 Telethon 的用户状态检测
  - 检测用户账号是否被删除或受限
  - 提升入群用户风险识别准确率
  - 与 CAS 黑名单检查形成双重防护

#### 用户清理功能重构 ⭐
- **基于 Telegram 官方标记识别**：完全重构用户清理功能，使用 Telegram 官方 API 标记识别异常用户
  - 识别已删除账号（Deleted Account）
  - 识别受限/封禁账号（Restricted/Banned）
  - 识别僵尸账号（长期不活跃）
  - 更安全、更准确的清理策略
- **移除废弃函数**：清理旧的用户检测相关函数，代码更清晰

### Bug 修复

#### 垃圾消息管理优化
- **管理员确认后永久封禁**：管理员确认垃圾消息后，改为永久封禁用户而非临时禁言
  - 避免垃圾发送者在解禁后继续骚扰
  - 提升群组安全防护强度

#### 规则引擎优化
- **重复字符检测阈值调整**：将重复字符检测的长度阈值从 10 提升到 20
  - 减少对正常重复表达的误判（如"哈哈哈哈哈哈"）
  - 保持对刷屏行为的有效识别
- **手机号正则优化**：手机号正则限定前后无数字，避免长数字串误判
  - 修复误将订单号、ID 等长数字识别为手机号的问题
  - 提升规则引擎准确率

### 文档更新
- 更新 `/help cleanup` 命令说明文档，反映新的清理策略

### 代码质量
- 修复 Ruff 代码风格警告
- 修复 isort/Black 格式问题
- 代码重构和优化

### 验证通过
- ✅ Ruff: 代码风格检查通过
- ✅ Mypy: 类型检查通过
- ✅ 功能测试：CAS 检测、用户清理、管理员确认流程测试通过

## [1.4.1] - 2026-05-20

### 新增功能

#### AI Vision 直判图片 / 贴纸垃圾 ⭐
- **多模态视觉检测**：图片与贴纸消息启用 AI Vision 直判路径
  - 图片 + caption + 群组对话上下文一次性送入多模态 AI
  - 返回 `is_spam` / `confidence` / `reason` / `extracted_text`
  - 节省一次 OCR 调用，提升整体效率
- **保留完整视觉信息**：二维码、logo、版式、水印不再因 OCR 仅提取文字而丢失
- **自动降级机制**：主备 provider 任一失败或模型不支持 Vision 时，自动回退到原 OCR → 文本管道，零功能退化
- **Provider 能力检测**：`AIServiceProvider` 新增 `supports_vision` 属性与 `detect_image` 方法，模型名判定兼容 OpenRouter 等带前缀的 provider（如 `openai/gpt-4o-mini`）
- **熔断复用**：`HybridAIDetector.detect_image_with_context` 主备回退 + 熔断状态共享
- **贴纸处理增强**：`on_sticker_message` 传入 emoji + `set_name` 组装 caption；放宽本地 OCR 门闩，Vision 可用时即允许进入贴纸检测流程
- **新增配置项**：
  - `AI_SPAM_VISION_ENABLED` - 是否启用 Vision 直判
  - `AI_SPAM_VISION_DETAIL` - 图像细节级别（low/high/auto）
  - `AI_SPAM_VISION_MAX_IMAGE_BYTES` - 单张图片最大字节数
  - `AI_SPAM_VISION_TIMEOUT` - Vision 请求超时

#### 规则引擎阈值化判断
- **配置驱动判定**：移除硬编码的垃圾标记逻辑，改为根据 `spam_threshold_rule` 动态判断
- **CRITICAL 级别例外**：仅 CRITICAL 级别规则直接标记为垃圾，其余规则走阈值判断
- **正则规则纳入阈值**：正则匹配命中后同样按阈值判定，统一行为，便于调优

### Bug 修复
- **加入请求 Redis 去重**：防止 Telegram 短时间内重复推送加入请求时触发重复的用户检测
- **修复帮助信息 HTML 转义**：
  - `activityskip` 帮助文本中的 `>=`、`>`、`<` 符号正确转义，避免 HTML 解析错误
  - `activity` 帮助文本更新为当前实际规则（非文本不扣分，需活跃度才能发送）

### 文档与配置
- README 版本徽章改为动态获取最新 tag，无需每次发布手动更新
- `.gitignore` 新增 `.wrangler/`（Cloudflare Pages 本地缓存）与 Zed 编辑器配置

### 验证通过
- ✅ Ruff: 代码风格检查通过
- ✅ Mypy: 类型检查通过
- ✅ AI Vision 主备回退、熔断复用、自动降级 OCR 测试通过

## [1.3.0] - 2026-04-19

### 新增功能

#### 宵禁模式
- **时间段限制**：支持设置宵禁时段（如 23:00-07:00），根据用户活跃度分级限制发言
- **活跃度分级限制**：
  - 活跃度 = 0: 无法发送任何消息
  - 活跃度 < 10: 无法发送非文本消息（图片、视频、贴纸等）
  - 活跃度 >= 10: 可正常发送消息
- **群组级时区配置**：每个群组可独立设置时区偏移（-12 到 +14），默认 +8
- **跨天时间支持**：支持跨天时间段（如 23:00-07:00）
- **自动通知**：进入/退出宵禁时段自动发送群内通知
- **后台调度器**：每分钟自动检查所有启用宵禁的群组
- **命令管理**：
  - `/curfew` - 查看当前宵禁状态
  - `/curfew <开始时间> <结束时间> [时区]` - 启用宵禁（如 `/curfew 23:00 7:00 +8`）
  - `/curfew off` - 禁用宵禁
- **时间格式灵活**：支持 HH:MM 或 HH 格式，分钟可选
- **命令自动完成**：已添加到群组管理员命令列表
- **帮助文档**：已添加到 `/help curfew` 帮助系统

### Bug 修复
- 修复宵禁模式 HTML 解析错误（`<` 和 `≥` 符号转义）

### 数据库变更
- 新增数据库迁移 `005_add_curfew_mode.sql`
- 在 `groups` 表添加 6 个宵禁相关字段

### 验证通过
- ✅ 代码格式化和 Lint 检查通过
- ✅ 功能测试通过（时间解析、权限限制、通知发送）
- ✅ 已合并到 dev 分支

## [1.2.2] - 2026-04-03

### 新增功能
- AI provider 长连接生命周期管理：支持空闲超时和最大存活时间自动重建 HTTP client
- AI provider 超时重建机制：防止长期空闲的 HTTP client 导致请求失败

### Bug 修复
- 串行化 AI client 重建流程，避免并发重建导致资源竞争
- 增强 AI API 错误日志，便于排查 API 调用失败原因

### 测试
- 修复 AI detector 测试 lint 告警
- 修正短消息长度预过滤断言

### 代码质量
- 格式化 Python 代码

## [1.2.1] - 2026-03-22

### 新增功能

#### 垃圾消息管理员确认与 AI 工作流
- 支持管理员对疑似垃圾消息进行手动确认，避免一刀切误封。
- 在垃圾消息与举报消息提示中自动 @ 管理员，提升响应速度。
- 确认模式下，避免重复自动入库导致样本重复或标签冲突。

#### AI 入群验证与上下文增强
- 入群验证流程接入 AI 垃圾检测，并对短文本进行预过滤，减少无谓 AI 调用。
- AI 检测上下文中加入群组名称和简介，让模型更好理解群内语境，降低误判率。

#### 活跃度系统优化
- 活跃度衰减仅在活跃度低于 10 时触发，高活跃用户不再被过度惩罚。
- 非文本消息（图片、贴纸、转发等）不再扣减活跃度，仅作为“需要一定活跃度才能发送”的受限操作。
- OCR 识别与活跃度跳过垃圾检测策略打通，高活跃用户的图片消息更易放行。

#### CAS 黑名单检查集成
- 集成 Combot Anti-Spam (CAS) 黑名单检查，支持入群与消息阶段自动拦截高风险用户。
- CAS API 请求增加指数退避重试机制，网络不稳定时更加鲁棒。
- 新增 `CAS_MAX_RETRIES` 环境变量，用于配置 CAS 最大重试次数。

#### 中英文标准化文本长度检测
- 引入标准化长度计算：1 个汉字/全角字符 = 1 标准长度，2 个英文字符 = 1 标准长度。
- 使用 `SPAM_MIN_TEXT_LENGTH` 控制最小标准化长度（默认 10），让中英文消息在长度预过滤阶段获得更公平的阈值。

### Bug 修复
- 修复加入请求验证中 `HIDE_REQUESTER_MISSING` 错误，避免重复 decline 导致异常。
- 修复管理员确认垃圾时 AI 自动入库重复、未清理旧样本的问题，防止训练集污染。
- 修复 antispam 模块的一系列类型检查和 lint 问题，提升代码质量与一致性。
- 修复活跃度检查中对 Telegram 系统账号 `777000` 的特殊处理，避免误统计。
- 确保关联频道消息与频道马甲消息完全跳过 Bot 处理，避免对官方关联频道产生干扰。
- 修复管理员邀请进群时仍保留旧待验证状态的问题，避免超时任务误踢新成员。
- 修复 CAPTCHA 服务的条件判断逻辑，避免在未正确配置的情况下误启用。
- 移除多余的消息速率限制中间件，防止与全局节流逻辑叠加造成体验问题。
- 修复 OCR 识别后未正确尊重活跃度跳过垃圾检测阈值的逻辑。

### 文档与配置
- 更新 README 与 `.env.example`，补充 CAS 相关配置和 `CAS_MAX_RETRIES` 示例。
- 更新 `SPAM_MIN_TEXT_LENGTH` 的配置说明，明确标准化长度计算方式。
- 调整 Turnstile 配置字段，移除废弃字段并补充新密钥说明，使之与最新 WebApp 实现保持一致。

### 验证通过
- ✅ Ruff: 代码风格检查通过
- ✅ Mypy: 关键模块类型检查通过
- ✅ 手工验证：入群验证 + 反垃圾 + 活跃度 + CAS + 管理员确认 等关键流程测试通过

## [1.2.0] - 2026-02-12

### 新增功能

#### 反垃圾系统增强
- **高级正则规则引擎**：
  - 替代简单关键词匹配，支持复杂模式识别
  - 提升垃圾检测准确率和灵活性
- **文本长度预过滤**：
  - 过滤过短或过长的异常消息
  - 减少无效检测，提升性能
- **垃圾消息提示优化**：
  - 在提示中添加消息 ID，方便管理员追溯
  - 延长垃圾消息缓存时间至 1 天，避免重复检测
- **垃圾检测规则更新**：
  - 添加微信相关垃圾检测规则
  - 更新垃圾检测模式变体，提升覆盖率

#### OCR 服务增强
- **混合 OCR 服务**：
  - 支持多种 OCR 提供者（OpenAI、百度、EasyOCR、PaddleOCR）
  - 自动回退机制：云 OCR 失败时自动切换到本地 OCR
  - 提升 OCR 服务可用性和稳定性

#### 用户管理功能
- **@username 解析支持**：
  - 实现 @username → user_id 映射功能
  - 支持 @username 格式的用户提及解析
  - 管理命令可直接使用 @username 操作用户

#### 管理员反馈优化
- **/notspam 命令增强**：
  - 添加 /nospam 和 /unspam 别名，更符合使用习惯
  - 支持消息链接格式（t.me/c/xxx/xxx）
  - 正确处理误判反馈：删除旧正样本后再添加负样本
  - 移除阈值参数，简化使用

#### 模型训练优化
- **样本提取策略优化**：
  - 改进训练样本提取逻辑
  - 提高模型训练质量

### Bug 修复

#### 严重 Bug 修复
- **修复 OpenAI OCR 跨 event loop 错误**：
  - 问题：跨 event loop 使用导致 "Event loop is closed" 错误
  - 修复：正确管理异步资源生命周期
- **修复验证失败后重复 decline 错误**：
  - 问题：重复调用 decline 导致 HIDE_REQUESTER_MISSING 错误
  - 修复：添加状态检查，避免重复操作
- **修复验证拒绝和 AI 检测器的异步资源管理问题**：
  - 正确处理异步上下文管理器
  - 避免资源泄漏

#### 类型检查修复
- 修复 CallbackQuery.bot 的类型检查错误
- 修复 mypy 类型错误
- 提升代码类型安全性

#### 消息格式修复
- 修复命令错误消息中 HTML 实体未转义的问题
- 训练完成通知消息添加 HTML 解析模式
- 统一 Bot 实例的默认 parse_mode 配置

#### sklearn 警告修复
- 显式设置 `TfidfVectorizer` 的 `token_pattern=None`
- 消除 "The parameter 'token_pattern' will not be used since 'tokenizer' is not None" 警告
- 提升代码清晰度，明确表示使用自定义分词器

### 代码改进

#### 重构优化
- **重复字符检测算法改进**：
  - 从简单重复次数阈值改为长度+占比双阈值检测
  - 添加 `length_threshold` 参数（默认 10）：字符串长度阈值
  - 添加 `ratio_threshold` 参数（默认 0.7）：重复字符占比阈值
  - 新算法计算所有连续重复 2 次及以上的字符长度占比
  - 优化日志输出，显示占比和具体数值
  - 更准确识别刷屏行为，减少误判
- 删除 parse_user_from_message 未使用的 bot 参数
- /notspam 使用简化版 parse_message_link
- 移除不必要的关键词和调整置信度阈值
- 代码格式化（Black + isort）

#### 配置优化
- 添加 config volume 到 Docker Compose 配置管理
- 更新基础镜像和 Python 版本
- 移除安全审查报告和测试报告文件

### 文档更新
- 添加 AGENTS.md 符号链接到 CLAUDE.md

### 验证通过
- ✅ Ruff: 代码风格检查通过
- ✅ Mypy: 类型检查通过
- ✅ 功能测试：所有新功能正常工作

## [1.1.1] - 2026-02-04

### 新增功能
- **AI 负样本入库优化**：
  - 添加过滤条件，避免低质量样本污染训练数据集
  - 提高模型训练质量和准确率

### Bug 修复
- **修复 unban/unmute 导致用户被意外踢出的严重 BUG**：
  - 问题：解除禁言/封禁时错误调用了 `unban_chat_member`，导致用户被踢出群组
  - 修复：使用 `restrict_chat_member` 恢复权限，保持用户在群组内
- **修复视频贴纸检测缺少 pyav 插件的问题**：
  - 添加 `imageio[pyav]` 依赖支持 WebM 视频贴纸帧提取
  - 添加 FFmpeg 系统库支持视频解码
- **修复 PyTorch 镜像体积过大问题**：
  - 使用 CPU 版本的 PyTorch 替代 GPU 版本
  - 镜像大小减少约 2GB

### 代码改进
- **依赖优化**：
  - 移除未使用的 onnxruntime 依赖
  - 精简依赖树，减少安装时间
- **配置优化**：
  - 更新 .gitignore 文件以忽略 .cache 目录
  - 避免缓存文件污染版本控制
- **测试优化**：
  - 移除冗余的语言列表打印语句
  - 清理测试输出，提高可读性

### 验证通过
- ✅ Ruff: 代码风格检查通过
- ✅ Mypy: 类型检查通过
- ✅ Docker: 镜像构建成功，体积优化

## [1.1.0] - 2026-02-03

### 新增功能

#### 上下文一致性检测（降低误判率）⭐
- **回复链相关性检测**（优先级最高）：
  - 使用 Embedding 计算当前消息与被回复消息的语义相似度
  - 相似度 ≥ 0.5 → 降低 20% 垃圾判定置信度
  - 保护正常的问答对话不被误判
- **群组话题一致性检测**：
  - 计算与最近 10 条消息的平均语义相似度
  - 相似度 ≥ 0.7 → 降低 15% 垃圾判定置信度
  - 保护正常的话题讨论不被误判
- **设计原则**：只降低不提高（避免误判正常的话题转移）
- **工作流程**：
  ```
  传统三段检测 + AI检测（并行）
      ↓
  结果合并
      ↓
  活跃度置信度调整
      ↓
  上下文一致性调整 ⭐ 最后防线
      ↓
  最终判定
  ```

#### Embedder 功能增强
- **新增方法**：
  - `embed(texts)` - 异步生成嵌入向量（线程池执行，避免阻塞）
  - `compute_similarity(text1, text2)` - 计算两文本余弦相似度
  - `detect_context_consistency(text, context_messages)` - 检测上下文一致性
- **性能优化**：
  - 在线程池中执行 Embedding 计算，不阻塞事件循环
  - 支持批量处理和缓存

#### 配置增强
- **新增配置项**（5个）：
  - `CONTEXT_CONSISTENCY_ENABLED` - 是否启用上下文一致性检测（默认 true）
  - `CONTEXT_HIGH_SIMILARITY_THRESHOLD` - 高相似度阈值（默认 0.7）
  - `CONTEXT_CONFIDENCE_REDUCTION` - 置信度降低幅度（默认 0.15）
  - `REPLY_SIMILARITY_THRESHOLD` - 回复链相似度阈值（默认 0.5）
  - `REPLY_CONFIDENCE_REDUCTION` - 回复链置信度降低幅度（默认 0.2）

### 效果示例

**场景1：正常回复问题**
```
群组对话：
  用户A: 这个手机壳哪里买的？
  用户B: 淘宝搜 xxx → https://taobao.com/xxx

检测结果：
  - Stage 1: 垃圾（链接）置信度 0.85
  - 回复链相似度: 0.72（高度相关）
  - 调整后置信度: 0.65（降低 0.20）
  - 最终判定: 正常消息 ✅
```

**场景2：突然发广告**
```
群组对话：
  用户A: 这个 Python 库怎么用？
  用户B: 看官方文档吧
  用户C: 加微信xxx，低价VPN

检测结果：
  - Stage 1: 垃圾（关键词）置信度 0.95
  - 上下文相似度: 0.12（话题不相关）
  - 调整: 不降低置信度（避免误判话题转移）
  - 最终判定: 垃圾消息 ❌
```

### Bug 修复
- **修复 Dockerfile pip install 语法错误**：
  - 问题：`pip install easyocr>=1.7.0` 被 shell 解释为重定向，创建了 `=0.15.0`、`=1.7.0`、`=2.0.0` 文件
  - 修复：添加引号 `pip install "easyocr>=1.7.0" "torch>=2.0.0" "torchvision>=0.15.0"`

### 代码改进
- **Ruff 自动修复**：
  - 修复导入顺序问题
  - 删除未使用的变量
  - 使用更简洁的列表展开语法

### 文档更新
- **CLAUDE.md**：
  - 三阶段反垃圾检测管道 → 多层反垃圾检测系统
  - 详细说明完整检测流程（传统三段 + AI + 活跃度 + 上下文）
  - 新增"反垃圾检测最佳实践"章节
  - 更新版本信息：v1.0 → v1.1
- **README.md**：
  - 重构智能反垃圾章节，分为 4 个子系统
  - 详细说明每个阶段的技术细节和性能指标
  - 突出上下文一致性检测作为"最后防线"的作用

### 验证通过
- ✅ Mypy: 类型检查通过（57 个文件）
- ✅ Ruff: 代码风格检查通过
- ✅ 功能测试：上下文调整正常工作

## [1.0.3] - 2026-02-02

### 新增功能
- **Sentry 环境配置增强**：
  - 添加 `SENTRY_ENVIRONMENT` 环境变量支持开发/生产环境区分
  - 添加 `SENTRY_TRACES_SAMPLE_RATE` 环境变量支持性能监控采样率配置

### Bug 修复
- **修复 AI 检测失败被误判为正常消息的严重 BUG**：
  - AI 服务故障时不再将失败误判为"正常消息"
  - 防止失败样本污染训练数据集
  - 失败时自动降级到传统三阶段检测

- **修复 Docker 容器权限错误**：
  - 修复 Embedding 模型加载权限错误（Permission denied）
  - 为 appuser 创建 home 目录并配置缓存路径
  - 设置多个缓存环境变量（HF_HOME、TRANSFORMERS_CACHE、XDG_CACHE_HOME）
  - 所有缓存文件统一写入 `/app/data/.cache/`（持久化）

### 代码改进
- **统一网络错误类型定义**：
  - 将 `NETWORK_ERROR_TYPES` 提取为模块级常量
  - 在 Sentry 过滤和异常处理中复用
  - 捕获所有临时性网络错误（TelegramNetworkError、TelegramRetryAfter、ClientConnectionError 等）
  - 改进日志输出，显示具体异常类型

### 配置优化
- 添加 `.serena/` 和 `.tool-versions` 到 `.gitignore`
- 从版本控制中移除本地工具配置文件

### 验证通过
- ✅ make lint: All checks passed (Ruff + Mypy)
- ✅ Docker 容器正常启动
- ✅ 模型加载成功

## [1.0.2] - 2026-01-19

### 安全修复
- **依赖包安全漏洞修复**：
  - 升级 `filelock` 3.20.1 → 3.20.3（修复 CVE-2026-22701 TOCTOU 竞态条件漏洞）
  - 升级 `pyasn1` 0.6.1 → 0.6.2（修复 CVE-2026-23490 DoS 内存耗尽漏洞）

### 配置优化
- **Gitleaks 双层防护**：
  - 添加 `.gitleaksignore` 忽略历史 commit 中的示例数据
  - 在 README 中添加 `# gitleaks:allow` 内联注释防止未来误报
  - 精确标记每一行示例数据，避免整个文件白名单化

### 验证通过
- ✅ pip-audit: No known vulnerabilities found
- ✅ gitleaks: No leaks found
- ✅ make lint: All checks passed

## [1.0.1] - 2026-01-19

### 文档更新
- 更新 `/cleanup` 命令帮助信息，添加"安全模式"标识说明

### Bug 修复
- 修复帮助文本中的引号嵌套语法错误

### 代码质量
- 修复所有 Ruff lint 错误（删除未使用变量、使用内置类型代替已废弃类型）
- 修复所有 Mypy 类型检查错误（添加类型保护、明确返回值）
- 通过 56 个源文件的完整类型检查

## [1.0.0] - 2026-01-18

🎉 **第一个正式版本发布**

### 新增功能

#### 验证系统
- **私聊验证系统**：避免群内验证消息轰炸，验证在私聊中完成
- **共享引导消息机制**：30秒内多用户未启动 Bot，只发送一条引导消息
- **多种验证方式**：
  - 基础验证：数学题、滑块、问答、表情、图片、蜜罐、拼图
  - CAPTCHA：Turnstile、Friendly Captcha、hCaptcha、MTCaptcha、ALTCHA
  - 支持随机验证方式
- **验证超时处理**：自动踢出超时未验证的用户

#### 反垃圾系统
- **三阶段智能检测**：
  - Stage 1: 规则引擎（关键词黑名单、URL/链接检测、频率限制）
  - Stage 2: ML 分类器（TF-IDF + SVM，准确率 ~90%）
  - Stage 3: 语义分析（bge-small-zh Embedding，准确率 ~98%）
- **编辑消息反垃圾检测**：应对垃圾发送者先发普通消息后编辑成垃圾的手段
- **OCR 图片识别**：使用 PaddleOCR 检测图片中的垃圾文字
- **AI 垃圾检测**：支持 OpenAI 兼容 API 进行并行检测
- **反频道马甲**：禁止用户以频道身份发言
- **活跃度系统**：
  - 文本消息 +1 活跃度
  - 非文本消息（图片/贴纸/转发/链接）-2 活跃度
  - 高活跃度用户可跳过垃圾检测
  - 低活跃度用户无法发送非文本消息

#### 群组管理
- **用户清理功能**：
  - 清理已删除账号（100% 安全）
  - 清理很久不上线的用户（安全策略）
  - 支持预览、执行、分类清理
  - Redis 缓存成员列表（1小时 TTL）
- **管理命令**：踢人、禁言、警告、封禁、解除禁言
- **自动删除命令消息**：保持群组整洁
- **管理员反馈机制**：支持管理员纠正垃圾检测结果

### 性能优化

- **万人以上大群支持**：
  - 使用 `iter_participants` 分批流式获取成员
  - 自动处理 FloodWait 异常，最多重试 3 次
  - 每 1000 人休息 1 秒，避免速率限制
  - 支持 10 万+超大群组
- **网络错误过滤**：三层过滤机制避免 Sentry 日志轰炸
- **Redis 权限缓存**：减少 Telegram API 调用
- **线程池处理**：CPU 密集任务异步执行

### 安全特性

- **限制清理范围**：只清理已删除和很久不上线的用户，避免误伤
- **Telethon 代理支持**：自动检测环境代理配置（socks5/socks4/http）
- **Session 文件安全**：添加 .gitignore 规则防止泄露
- **模型签名验证**：防止恶意模型注入
- **强密码要求**：数据库和 Redis 必须设置强密码

### 技术栈

- Python 3.12+
- aiogram 3.x（Telegram Bot API）
- Telethon（Telegram Client API）
- PostgreSQL 16（配置/日志/样本存储）
- Redis 7（缓存/队列/TTL）
- scikit-learn（ML 分类器）
- sentence-transformers（语义嵌入）
- PaddleOCR（图片文字识别）
- Docker Compose（容器化部署）

### 部署支持

- Docker Compose 一键部署
- 开发环境热重载（watchfiles）
- 生产环境优化配置
- 健康检查（PostgreSQL）
- 数据持久化（volumes）

### 文档

- 完整的 README 使用文档
- .env.example 配置模板
- Docker 部署说明
- Makefile 快捷命令
- 代码注释完善

---

## 版本说明

### 版本格式：主版本号.次版本号.修订号

- **主版本号**：不兼容的 API 修改
- **次版本号**：向下兼容的功能性新增
- **修订号**：向下兼容的问题修正

### 变更类型

- **新增**：新功能
- **变更**：已有功能的变更
- **废弃**：即将移除的功能
- **移除**：已移除的功能
- **修复**：错误修复
- **安全**：安全相关的修复

---

[1.0.0]: https://github.com/cnsunyour/tg-guard-bot/releases/tag/v1.0.0
