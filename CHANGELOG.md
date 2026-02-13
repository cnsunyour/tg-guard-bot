# 更新日志

本项目的所有重要变更都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
