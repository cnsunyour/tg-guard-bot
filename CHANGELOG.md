# 更新日志

本项目的所有重要变更都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增功能

#### 数据定时清理：spam_samples 负样本裁剪 + audit_logs 保留期 🧹
- **背景**：`spam_samples` 与 `audit_logs` 长期只写不删无限增长——前者拖慢 ML 重训练并污染负样本池，后者磁盘与备份体积无界膨胀
- **spam_samples 策略**：正样本（`is_spam=true`）永久保留；负样本按训练取数比例裁剪，仅保留最新的 正样本数 × 20 条（比例提取为共享常量 `NEGATIVE_SAMPLES_PER_POSITIVE`，训练与清理强制同口径——**取数与保留共用 `(created_at, id)` 排序**，被删除的恰好是不参与训练的样本）；无正样本时跳过，避免全量误删；删除时二次校验 `is_spam=false`，防并发改标误删正样本
- **audit_logs 策略**：按 `AUDIT_LOG_RETENTION_DAYS`（默认 365 天）滚动删除过期记录，0 表示永久保留；安全事件回溯窗口受保留期约束（SECURITY.md 已同步说明）
- **实现**：新增 `src/services/data_cleanup.py` 后台调度器（启动即执行一轮，之后每 `DATA_CLEANUP_INTERVAL_HOURS`（默认 24）小时一轮）；**启动间隔守卫**：距上次成功运行不足 1 小时的重启（crash-loop/滚动部署）跳过首轮，时间戳存 Redis、故障降级放行；删除统一走 `core/database.delete_in_batches`（默认 5000 行/批从旧到新分批提交，避免长事务），spam 侧先一次性确定保留边界再按固定范围分批删（避免每批重算 top-K 保留集，并发新插入的负样本天然不受影响）；两类策略独立容错，任一失败不记录成功时间戳（下次重启守卫放行重试）；调度器任务异常死亡时自动复位状态可重启自愈；`DATA_CLEANUP_ENABLED=false` 可整体关闭
- **修正**：`get_training_data` docstring 中过时的「正样本的 10 倍」描述同步修正为与代码一致的 20 倍

### 代码质量

- 新增 `src/core/tasks.py` `spawn_background_task()`：fire-and-forget 后台任务强引用统一管理（防 asyncio 弱引用下任务被 GC 静默回收），`main.py` 启动阶段任务已迁移
- 新增 `tests/test_data_cleanup.py`（13 项）：裁剪比例、无正样本跳过、未超限 no-op、`prune(0)` 仓库层拒绝、保留期 cutoff 计算、永久保留跳过、策略间故障隔离、守卫拦截/放行/Redis 降级、失败轮不标记成功、调度器 start/stop 幂等、任务异常死亡自愈
- 修复 on_startup 测试隔离：`test_verification_startup_resume` 此前会真实启动数据清理服务连真实 DB（新增 mock）

### Bug 修复

- **关停竞态**：验证 timeout 等后台任务改经 `spawn_background_task` 统一强引用管理，进程关闭时于一切依赖关闭前统一取消（原实现任务可能在 DB/Redis/Bot session 关闭后唤醒，触发连接懒重建与竞态、重启窗口内待处罚用户逃逸）
- **恢复扫描健壮性**：deadline 键改按批 MGET（每批两次往返，500 会话从 1000 次串行 RTT 降至约 10 次）；SCAN 中途故障时已收集键继续处理不再丢弃；`{session}:{deadline_ms}` 值解析下沉为 `verification_recovery.parse_deadline_value` 唯一权威入口（消除第三处手写解析）；SCAN 匹配模式经 `RedisKeys.verification_deadline_pattern()` 集中管理
- **管理员 mention 缓存语义隔离**：`chat_admins:{chat_id}` 键更换为 `spam_handler_admins:{chat_id}`（过滤策略编入键名）——滚动部署期间新旧进程不再误用旧语义缓存，spam 提示的 mention 推送名额不再浪费在无处置权限的管理员上；函数更名 `get_spam_handler_admins_mention` 消除「全部管理员」误导
- **批量删除瞬态错误降级**：`TelegramNetworkError`/`TelegramServerError` 由「整批计失败」改为降级逐条删除（网络抖动时保住其余消息的删除）；删除结果文案「成功」改为「已处理」（deleteMessages 幂等口径：不存在/已删的消息也计入，旧措辞失真）；批累积改用 `itertools.batched` 消除双份 flush 逻辑

## [1.8.4] - 2026-08-25

### 新增功能

#### 群内验证引导消息匿名 mention 等待验证的用户 👤
- 未启动 Bot 的用户入群时，群内共享引导消息把该批用户匿名 mention 进首条消息（渲染为 👤 可点击链接，不暴露用户名）；只有随消息发出的 mention 才触发 Telegram 推送，提醒用户主动私聊完成验证
- 首条消息聚合同批入群用户（`VERIFICATION_HINT_AGGREGATION_DELAY`，默认 1.5 秒窗口）；窗口内晚到的用户经编辑补进消息（视觉补全，Telegram 不为编辑新增的 mention 推送提醒）
- 单条消息 mention 上限 5 个（对齐 Telegram「仅前 5 个 mention 触发通知」）；加入请求（Approve New Members）模式不 mention（用户尚未入群，收不到群消息）
- mention 相关 Redis 调用全部可降级：故障时退回无 mention 的原引导消息，不阻断验证主流程

### Bug 修复

#### 入群资料检测修复 bio 获取链 🔍
- **问题**：入群时对用户资料（名字 + bio）的 AI 反垃圾检测长期拿不到 bio——Bot API `getChat` 仅对曾与 Bot 私聊交互过的用户返回 bio（tdlib/telegram-bot-api#839），而入群场景下用户几乎必然未启动过 Bot，垃圾账号得以把广告放 bio 里绕过入群检测；启用「批准新成员」的群还会丢弃加入请求事件自带的 bio 字段
- **修复**：
  - join_request（批准新成员模式）：直接使用 `ChatJoinRequest` 事件自带的 bio 与用户名字，不再依赖 getChat
  - join（直接入群模式）：`ChatMemberUpdated` 事件无 bio 字段，新增 Telethon `get_user_bio`（经群组上下文解析实体后取 full user 的 about），失败降级 `getChat` 兜底，两级都拿不到则只检测名字，不阻断验证流程
- **注意**：join 模式的 Telethon bio 路径需开启 `USER_STATUS_CHECK_ENABLED` 并配置 Telethon session；未开启时自动降级为仅 getChat（行为同旧版）

### 代码质量

- 新增 `tests/test_verification_profile_check.py`（13 项）：bio 来源选择、全部降级路径、入群流程字段透传、`get_user_bio` 分支
- 新增引导 mention 跨窗口登记语义测试（2 项），固化「上一窗口用户不进下一条消息」的语义

## [1.8.3] - 2026-08-20

### Bug 修复

#### /spam 封禁失败不再阻断消息处理与样本入库 🗑️
- **问题**：管理员对已被踢出群的用户执行 `/spam` 时，`ban_user` 返回 `user_not_in_chat`，而删除消息、写入训练样本、自动训练全部包在 `if result.success` 内，导致整条命令被阻断——垃圾消息既不删除也不进训练库
- **根因**：`cmd_spam` 管理员分支调用 `ban_user` 时遗漏 `allow_left=True`（举报人工复核与 antispam 链路均已传），默认行为会先校验目标在群内，`left`/`kicked` 直接失败返回
- **修复**：
  - 补 `allow_left=True`，目标已退群/被踢仍可拉黑
  - 封禁降级为 best-effort：删消息 / 入训练库 / 自动训练 / 回复脱离 `result.success`，仅 `target_is_admin` 硬阻断；`verify_user_failed`、`verify_admin_failed`、`operation_failed` 等 API 故障同样不再连带丢弃管理员已明确表态的标注
  - 移除 handler 层重复的管理员预检查，统一由 `ban_user` 内 `verify_not_admin` 负责，少一次 API RTT 并消除两次查询间的状态竞态
  - `-d` 封禁失败时 `revoke_messages` 未生效，退化为删除被回复的单条消息
  - 自动训练仅在样本成功入库后触发
- **新增文案**：`moderation.spam.processed_ban_failed.message`（三语），如实告知「消息已处理但用户未封禁」及失败原因，不再谎报封禁成功

#### 封禁审计写入失败不再误报为封禁失败 📝
- **问题**：`ban_user` 将 `ban_chat_member` 与 `AuditRepository.log_action` 放在同一 `try` 中，Telegram 侧封禁已成功但审计入库失败时仍返回 `operation_failed`
- **影响**：调用方据此判定「未封禁」并执行补偿动作，向管理员报告错误状态
- **修复**：审计写入移出主 `try`，失败仅记录 error，不翻转处罚结果返回码

### 代码质量

- 新增 `tests/test_moderation_spam_command.py`（10 项），覆盖 `/spam` 管理员分支契约：四种封禁失败码仍删除并入库、`target_is_admin` 硬阻断、`-d` 成功/失败的删除分支、样本入库失败跳过自动训练、`allow_left=True` 传参回归
- 修复 `test_backup_failure_also_marks_for_rebuild` 依赖真实 API 的伪绿：原测试第二段只注入 backup 失败，primary 未失败时会真实调用并直接返回、走不到 backup 分支；改为同一次 `detect` 内注入主备双失败，测试不再发出真实网络请求

## [1.8.2] - 2026-08-18

### 新增功能

#### 数据库迁移切换到 Alembic 🗄️
- **背景**：原方案为自研 `create_all` + 手写 SQL 双轨，缺乏版本化迁移与回滚能力，且 CLAUDE.md 声称用 Alembic 但实际零配置（失实描述）
- **新增**：完整 Alembic 迁移体系
  - baseline `c3d35c9d5221` 涵盖全部初始表 + 索引 + FK + server_default（对齐旧库 DDL）；downgrade 抛错防静默假成功留孤儿表
  - `docker-entrypoint.sh` 启动时自动 `alembic upgrade head`，PG advisory lock 防并发；检测「有业务表但无 alembic_version」的旧库自动 stamp baseline，避免 DuplicateTable 启动失败
  - 异步迁移环境（`create_async_engine` + `NullPool`，独立 engine 不复用应用连接池）
  - `make db-migrate` / `db-revision` / `db-down` 命令经容器内执行
- **影响**：数据库 schema 变更从此走版本化迁移；模型补齐 9 列 server_default 与显式索引对齐旧库
- **废弃**：删除自研 `scripts/migrate*.py`；旧 7 个 SQL 归档 `migrations/legacy_sql/`

### Bug 修复

#### 反垃圾 review 待审提示回归回复原文 💬
- **问题**：确认模式（spam_confirm）的待审核提示正文复制被检测原文预览，群内消息冗余且预览可能展开链接卡片
- **修复**：提示以 reply 关联被检测消息（原消息已删则降级普通消息），原文由 Telegram 回复引用展示；提示正文与复核完成后的编辑统一关闭网页预览

#### /report 举报提示不再复制被举报内容 💬
- **问题**：与 review 提示同源——群内待处理提示正文复制被举报内容预览，冗余且举报原因中的链接会在编辑完成态渲染出预览卡片
- **修复**：提示仅以 reply 关联被举报消息定位原文，正文不复制内容预览；完成态三条路径（成功/业务失败/异常）统一关闭网页预览

#### /lang 菜单幂等编辑不再误报错误 🔧
- **问题**：生产日志报 `edit_text` 在菜单内容未变化时抛 `message is not modified`，被误判为 ERROR 并弹出「无法更新语言菜单」错误 toast，但 locale 实际已保存成功
- **修复**：新增 `_is_message_not_modified` 判定（类型 + 错误文本双校验）；幂等编辑静默降级为 debug 日志并提示保存成功，其他编辑失败降级为 warning 且不再显示失败 toast

### 代码质量

#### 杂项 🧹
- `.gitignore` 收紧 `*.sql` 规则，改为只忽略备份产物（不再误忽略 Alembic 迁移相关 SQL）

## [1.8.1] - 2026-08-12

### Bug 修复

#### 入群限制权限收紧，堵住 react/编辑头衔漏洞 🔒
- **问题**：新成员入群限制发言时，`ChatPermissions` 仅设 `can_send_messages=False`，未显式禁用 `can_react_to_messages`（消息表态）、`can_add_web_page_previews`、`can_manage_topics` 等字段；Telegram 对未显式声明为 False 的权限字段默认放行，未验证用户可对群消息表态或发起话题，绕过验证门槛
- **修复**：限制期 `ChatPermissions` 仅保留必要字段，显式禁用 react / 编辑群头衔 / 发起话题 / 链接预览等全部发言相关权限；`moderation.py` 禁言路径同步对齐
- **影响**：未通过验证的用户在限制期不再能表态、编辑群头衔或发起话题

#### datetime.utcnow() 弃用替换与时区脆弱性消除 ⏱️
- **问题**：代码大量使用 Python 3.12 已弃用的 `datetime.utcnow()`（返回 naive datetime）；aiogram 的 `until_date` 序列化在非 UTC 服务器上对 naive datetime 调用 `timestamp()` 时按本地时区计算，导致禁言/限制时长偏差 8 小时（生产 Docker 默认 UTC 偶然正确，开发机 +08:00 可复现 8 小时偏移）
- **修复**：新增双函数架构 `src/core/utils.py`——感知时区的 `utcnow()`（对应 Telegram API / timestamp 计算的外部世界）+ naive 的 `utcnow_naive()`（对应 asyncpg + TIMESTAMP WITHOUT TIME ZONE 的数据库世界）；全量替换 ORM 模型默认值、Repository 查询、Handler `until_date` 调用、AI 检测器进程内计时
- **影响**：所有时区下禁言 / 限制 / 超时计算一致；ORM 向 PostgreSQL 写入不再有 aware/naive `TypeError` 隐患

#### altcha PHP 版本约束对齐依赖实际要求 📦
- **问题**：captcha-webapp 的 altcha PHP 依赖下限声明 `>=7.4`，但 altcha 实际运行时要求 `>=8.2`，低版本 PHP 部署报错
- **修复**：版本约束对齐至 `>=8.2`

### 代码质量

#### README v1.8.0 文档对齐 📝
- 路线图追加 v1.8.0 条目、代码结构导航更新（`ai_protocols` / `ai_contracts`）、AI 多协议配置说明补充

## [1.8.0] - 2026-08-11

### 新增功能

#### AI 检测支持多协议（OpenAI Responses + Anthropic Messages）🧠
- **背景**：原 AI 反垃圾检测仅支持 OpenAI Chat Completions 协议，无法接入 Anthropic Claude 与 OpenAI Responses API
- **新增**：引入 `ProtocolAdapter` 模式收敛三协议差异（端点 / 认证 / 请求体 / 响应解析 / 结构化输出），保留 OpenAI Chat Completions 向后兼容
  - **Anthropic 双模式**：native `output_config.format`（Claude 4.5+ GA）/ Tool Use（全模型，`strict:True` 保证 schema）
  - **结构化输出配置**：`structured_output_mode`（auto/strict/legacy）+ `anthropic_output_mode`（auto/native/tool），保护 DeepSeek / Moonshot / OpenRouter 等第三方兼容接口
  - `openai_chat` + `auto` 默认 `legacy`，确保默认配置与改造前行为一致
  - 终止类响应（refusal / max_tokens / content_filter）不重试同 provider、不计熔断、直接走备份
  - JSON 兜底降级：Markdown fence 剥离 + `JSONDecoder.raw_decode`（替代脆弱正则）
- **影响**：AI 反垃圾支持主备服务商异构配置与自动回退（如主 OpenAI Responses + 备 Anthropic Messages）
- 新增 `src/ml/ai_protocols.py`、`src/ml/ai_contracts.py`、`tests/test_ai_protocols.py`（23 个协议测试）

## [1.7.2] - 2026-08-08

### 新增功能

#### 四管理员命令支持带参直接设置 ⚡
- **场景**：`/setverify`、`/activity`、`/antispam`、`/antichannel` 此前无参时仅弹 inline 面板，开关切换需点按钮；管理员常希望一行命令搞定
- **新增**：四个命令均支持带参数直接设置（如 `/setverify on`、`/activity off`），无需走 inline button；无参时行为不变（仍弹面板）；非法参数友好提示用法

#### /report 举报提示引用被举报消息并附内容预览 💬
- **场景**：管理员收到举报提示时，需上滑翻找被举报的原消息才能判断，移动端尤其不便
- **新增**：`/report` 举报提示改为 reply 到被举报消息，并附被举报消息内容预览（含非文本类型标注），管理员无需翻页即可看到上下文

### 安全修复

#### 全项目安全审查加固（Top 5 + 2 项边界）🔐
- **M2 `/cleanup` TOCTOU 批量踢人**：入口改 strict 权限校验，各执行子命令（deleted/restricted/scam/fake）扫描后重新检查操作者权限，防止扫描缓存 TTL 期间权限被撤销仍批量操作
- **M1 白名单中间件覆盖 `chat_join_request`**：此前加入请求模式未经白名单校验；`/whitelist remove` 成功后自动退群（不注册 `chat_member`，避免退群副作用放大）
- **M3 proxy 日志脱敏**：`_redact_proxy_url` 统一剥离 userinfo，`_proxy_has_auth` 仅输出布尔值；覆盖无协议 `user@host:port` 与 urlparse 误识 `user:` 为 scheme 的边界
- **M4 CAPTCHA 签名密钥条件校验**：配置 `CAPTCHA_WEBAPP_URL` 时 `CAPTCHA_SIGNATURE_KEY` 须 ≥32 字符（webapp_url 是所有 provider 回调的先决条件，含 `/setverify turnstile` 显式选择路径）
- **L1 `/verifyconfig` 管理员权限**：缓存版权限校验，非管理员不可查看配置
- codex review 额外发现 2 项 P1 边界：proxy 无协议 `user@host:port` 泄露 + `/setverify turnstile` 显式选择绕过 `webapp_captcha_enabled` 前提

#### 安全审查剩余 7 项 Low / 可选加固 🔧
- **L2 Telethon session 文件安全**：`lstat` + 拒绝符号链接 + group/others 可读告警
- **L3 禁言时长解析严格化**：`parse_duration` 改用 `parse_time_to_seconds` 严格 `fullmatch`；非法时长（`30mxxx`/`0m`/`abc`）抛 `ValueError` 不再误判永久禁言；`cmd_mute` 捕获后提示用法
- **L4 反垃圾反馈解析加固**：`spam_feedback` 精确 4 段解析 + `feedback_type∈{normal,spam}` 白名单 + 正整数 ID
- **L5 Sentry `before_send` 清洗**：清洗整个 event + 字段名过滤（连字符归一化覆盖 `X-API-Key`/`private-key` HTTP header）+ depth 20 + markers 扩展
- **L6 自定义规则配置上限**：文件 ≤1MB / 规则数 ≤200 / pattern ≤500 字符；编译失败/重复 ID 自动剔除，运行时不再抛 `re.error`
- **L7 Redis 明文密码消除**：`-a` 明文 → `REDISCLI_AUTH` 环境变量（`backup.py` subprocess env 传 + `docker-compose.prod.yml` healthcheck 去 `-a`）
- **M5 MTCaptcha token 日志删除**：移除 verify 函数 token 输出（保留 GET，官方未证实 POST 契约）
- 全项目审查整体 **0 Critical / 0 High**；gitleaks 537 commits 零泄露、trivy 零漏洞；残余 L6 ReDoS 与 M5 URL query 为已接受风险

### 代码质量

#### aiogram 下限收紧至 3.25 📦
- 项目所用 API（`DefaultBotProperties`/`ReplyParameters` 3.7、`InaccessibleMessage`/`MessageOrigin*` 3.6）在 3.25 齐备；3.25→3.30 的 breaking 均不命中项目用法（`hide_requester` 经 `extra="allow"` 透传，非正式字段）

## [1.7.1] - 2026-08-07

### 新增功能

#### /lang 命令支持直接参数切换语言 🌐
- **问题**：匿名管理员（sender_chat==chat）无法通过 inline button 回调切换语言——Telegram 的 callback_query 不携带 sender_chat，无法识别匿名管理员身份，导致 /lang 按钮对匿名管理员不可用
- **新增**：`/lang <locale>` 直接通过消息路径切换（`check_admin_permission` 可检测匿名管理员），匿名管理员输入 `/lang en` 即可；别名归一化（zh-HK→zh-Hant、en-GB→en），无效 locale 友好提示
- button 路径与参数路径共用 `_persist_locale`，原有校验与菜单编辑保留

### 代码质量

#### on_* 处理器前置统一重构 ♻️
- 抽取 `_run_message_prechecks` 公共函数 + `SkipReason` 枚举，统一 11 个 on_* 消息处理器的预处理逻辑（命令注册检查、频道消息过滤、权限等）
- 修复 on_photo 频道路径遗漏（重构前未走统一前置，频道图片可绕过反垃圾检测）
- 命令检查前移到频道过滤之后，堵住 anti-channel 绕过
- 新增 `tests/test_antispam_prechecks.py`（34 个测试）锁定前置过滤行为，确认重构无回归
- 用户名映射更新改为 best-effort

## [1.7.0] - 2026-08-07

### 新增功能

#### i18n 多语言支持（zh-Hans / zh-Hant / en） 🌐
- 扁平点分 catalog（`locales/{locale}.json`）+ `{var}` 占位符；启动校验重复 key / 占位符 parity / Telegram HTML 白名单
- `LocaleMiddleware` 注入 `BoundLocalizer`，handler 经 `localizer.t(key, **vars)` 访问文案；`LocaleResolver` 解析群组/用户/跨目的地语言偏好（不依赖 ContextVar，适配异步/延迟/定时任务）
- 全量出站文案 i18n：验证流程、反垃圾检测/确认/反馈、群管命令、宵禁、管理配置、WebApp 验证页
- 业务数据用稳定 code 持久化（如 `system:spam`、`system:channel_impersonation`），展示层按 locale 渲染——群切语言后旧记录不显示旧语言
- `/lang` 命令切换群组/用户语言，按钮用 endonym 自称（简体中文/繁體中文/English，不随当前语言变）
- locale 地区别名映射（zh-CN→zh-Hans 等）；rehydrate 对不支持 locale 容错（启动防崩溃）

#### 反垃圾确认/举报提示新增「忽略」按钮 ⏭
- 自动检测 review prompt 与群成员举报 prompt 新增第三按钮「忽略」：不处罚、不入库、仅关闭本次提示
- approve/reject 同行、ignore 独立一行（移动端三按钮同行过窄）

#### review prompt 自动清理 TTL 配置 ⏱
- 新增 `SPAM_REVIEW_PROMPT_AUTO_DELETE_SECONDS`（默认 3600s）：未处理的确认提示到期自动清理，不处罚、不写入训练样本；Redis state 同步过期，prompt 与 state TTL 必须一致（否则旧 state 因 SET NX 阻止同一消息重建 review）

### Bug 修复

#### 反垃圾确认提示失败/已处理时残留 🔧
- **问题**：自动检测的 review prompt 在 ban 失败、状态已被他人消费、或异常路径下，callback 仅弹 toast，提示消息保留按钮残留，管理员需手动删；恶意 bot 连发两条垃圾、管理员处理第一条后第二条提示因原消息已删报 message not found
- **修复**：`on_spam_review_callback` 改 try/finally 统一清理——进入处理后无论成功/业务失败/异常，finally 始终 edit_text 移除按钮 + auto_delete(30)；单次 answer 契约（前置失败各自 toast，通过前置则 processing toast 防超时）；ban 显式 revoke_messages=False/allow_left=True；ban 失败审计 `spam_review_ban_failed` 含 error_code

#### 举报提示按钮失败/已处理时残留 🔧
- **问题**：`/spam`、`/report` 举报提示的 approve/reject 按钮在失败（ban 失败、举报已被命令路径处理、异常）时仅 `callback.answer(alert)`，提示消息按钮原样保留残留
- **修复**：抽取 `_handle_report_callback` 公共函数（approve/reject/ignore 共用），同款 try/finally 统一清理；新增 `_process_report_ignore`（status=ignored）与 `on_report_ignore`；提示 auto_delete delay 对齐 review（复用 `SPAM_REVIEW_PROMPT_AUTO_DELETE_SECONDS`）；ban 显式 revoke_messages=False/allow_left=True；`update_report_status` 返回值检查

#### i18n 出站文案多轮审查补迁 🔧
- reason code 化：warn reason / rule description / moderation 默认 reason 改稳定 code 持久化，展示层按 locale 渲染
- Vision 提示占位拆分（recognized_text 截断 200 后 escape）
- 验证码题库三语翻译 + WebApp 第三方组件 locale 跟随
- suspicious_platforms 繁体用词修正（opencc **s2tw** 校验，非 s2t 避免 羣/喫 假阳性）
- content_type 等库枚举不嵌入本地化文案（避免中英混排）；标点 i18n（顿号」、「对应 en ", "）
- codex 全新 session 双向审查（找遗漏 + 验证修复副作用），补迁训练样本回归等 5 处

### 代码质量

- 📝 CLAUDE.md / README 新增 i18n 多语言章节（架构 / 新增出站文案守则 / 配置 / 命令 / 路线图）
- 📝 memory 记录版本 bump 流程、i18n 迁移项目、callback 权限陷阱、群消息用户名脱敏约定等

## [1.6.4] - 2026-07-24

### Bug 修复

#### 修复举报按钮权限绕过 🔐
- **问题**：`on_report_approve` / `on_report_reject` 把 Bot 发送的 `callback.message` 传给 `check_admin_permission_strict_message`，该校验 `message.from_user.id`——而 `callback.message.from_user` 是 Bot 自身（必为群管理员），守卫恒真，任意普通成员可点击「接受/拒绝」执行封禁/删除/入库或拒绝举报
- **修复**：改为 `check_admin_permission_strict(bot, chat_id, callback.from_user.id)` 严格校验实际点击者（直接 API 查询、不信任缓存）；API 异常 fail-closed 拒绝
- 新增 `tests/test_moderation_callbacks.py` 回归测试，锁住「权限检查使用点击者 ID 且拒绝后不进入业务处理」

#### 入群处理新增 in-flight 互斥锁，修复 AI 慢请求期间重复触发检测 🔧
- **问题**：AI 反垃圾检测慢请求/超时可能耗时远超 60s dedup 窗口，且 pending 锁在 `check_user_spam_info` 之后才建立，用户重复点击入群请求可绕过去重，重复触发 CAS/状态/AI 检测
- **修复**：
  - 新增 `_verification_inflight_lock`（`SET NX EX` + owner token，Lua compare-and-delete 释放），覆盖 CAS/状态/AI 整段处理窗口，上次未处理完则拒绝重入
  - `on_join_request` / `on_user_join` 前移 pending/approved 快速路径到昂贵检测之前，已建立验证后不再重复调用 AI
  - 两入口使用独立 inflight 键，避免批准加入后正常入群事件被误锁
  - 新增配置 `VERIFICATION_INFLIGHT_TTL_SECONDS`（默认 300s）
- 新增 11 项锁单元测试与入口集成测试

#### 群消息中管理员/操作者名称改为完整显示 🔧
- **问题**：管理员邀请入群欢迎消息、反垃圾确认/反馈提示中，管理员与操作者名称误用脱敏函数 `format_user_mention`，显示为「由管理员 `张**三` 邀请」「操作者: `李**四`」
- **修复**：新增 `format_trusted_user_mention()`（`src/core/utils.py`）作为可信用户名称的统一不脱敏入口（完整显示名 + @username|ID，仅 `escape_html`），替换 `verification.py` 邀请者、`antispam.py` 两处操作者共 3 处误用；被邀请/被处理的普通用户仍走 `format_user_mention` 脱敏
- 补 `format_trusted_user_mention` 单元测试（完整显示 / 无 username 回退 / HTML 转义）

#### CAS API 失败日志记录异常类型与超时阶段 🔧
- **问题**：CAS 服务用 `str(e)` 记录请求失败原因，而 httpx 超时/网络异常的 `str(e)` 为空，导致「CAS API 请求失败」日志丢失原因，无法判断是超时还是其他网络错误
- **修复**：
  - 新增 `src/core/http_errors.py` 公共 httpx 异常格式化模块：始终输出异常类名、细分超时阶段（connect/read/write/pool）与有效秒数、HTTP 状态码；safe 模式安全提取响应体白名单字段并脱敏 URL
  - CAS 接入格式化：失败日志现输出 `[error_type=X] [phase=Y] [timeout_seconds=Z]` 并附带 `__cause__`；AI `_format_error` 委托公共函数（输出与改造前等价）
- 补充 http_errors / CAS 超时 / AI 格式化回归测试

### 代码质量

- 📝 CLAUDE.md 同步入群处理 in-flight 互斥锁机制：补充三层去重（dedup/inflight/pending）说明、核心函数、Redis 键示例、`VERIFICATION_INFLIGHT_TTL_SECONDS` 配置项与陷阱条目

## [1.6.3] - 2026-07-19

### Bug 修复

#### 验证成功流程改用网络重试，彻底移除邀请链接 🔧
- **问题**：v1.6.2 收紧邀请链接后暴露多处既有缺陷：
  - `handle_verification_success`、文本验证码、`on_user_join` 共 4 处 `restore_user_permissions` 调用忽略返回值，恢复失败仍发送成功文案与欢迎消息
  - Web CAPTCHA 等路径 `approve_chat_join_request` 失败仍通知"已批准"
  - restricted 用户点击"加入群组"邀请链接无效（已在群内不会触发重新入群）
  - `on_join_request` 的 approved_key 恢复路径批准成功后过早删除标记，与随后的 `on_user_join` 事件竞争，导致已验证用户加入后又被要求验证
- **修复**：
  - 移除 `send_verification_success_message` 的全部邀请链接逻辑，改为 `success` / `success_join_request` / `restore_failed` / `approve_failed` 四种纯文本消息
  - 新增 `retry_async_call`（`src/core/retry.py`），对权限恢复与加入请求批准做网络错误重试（最多 3 次，指数退避）
  - 改造全部 8 个调用点：恢复/批准失败时发送降级文案、提前返回、保留 `approved_key`
  - 统一 `approved_key` 生命周期：写入者不删、approve 成功不删（留给 on_user_join restore）、restore 成功才删、失败保留
  - 文本验证码与 `handle_verification_success` 的 normal 路径补写 `approved_key`（原本缺失，导致失败后用户重新入群仍被要求验证）
  - 管理员邀请/批准分支补消费残留的 `approved_key`

## [1.6.2] - 2026-07-18

### Bug 修复

#### 验证成功后的邀请链接收紧到 `failed_restore` 场景 🔧
- **问题**：此前每位用户验证通过后都会创建一次性邀请链接并附带"点击加入"按钮。但 normal 模式用户一直在群里（仅权限被限制后恢复）、join_request 模式 `approve_chat_join_request` 已让用户加入，这两种场景的链接纯属冗余，且 normal 模式追加文案“💡 如果没有自动加入”对从未离开群的用户逻辑不通
- **调整**：仅在 `failed_restore`（`restore_user_permissions` 失败）时尝试创建链接供用户手动重新加入；并拆分创建与发送的异常处理（日志区分二者）、降级文案改为明确引导联系管理员

## [1.6.1] - 2026-07-17

### 代码质量

- ✅ 修复 `make check` 报告的全部问题，检查链路恢复全绿（mypy 0 错误、pytest 120 passed、0 warning）
  - 补充类型注解消除 9 处 mypy 错误：`member_query` 的 `result` 字典注解、宵禁二次查询改名 `current_group`、PIL 图片变量函数级前置声明 `img: Image.Image`、异常状态通知兜底 `reason or "未知状态"`
  - CAS 群组通知测试改为负向断言，对齐 [1.6.0] 移除违规次数展示的产品决策
  - 删除 `conftest.py` 冗余 `event_loop_policy` fixture，消除 `pytest-asyncio` 弃用警告
  - `verification.py` 两处 `logger.info` 合并（black 格式化）

## [1.6.0] - 2026-07-17

### 新增功能

#### 入群短窗口消息防护中间件 🛡️
- **场景**：新成员入群后、`restrict_chat_member` 权限真正下发生效前的短暂窗口里仍可抢发消息，既有反垃圾链路拦截不到
- **实现**：`on_user_join` 在绝对第一步写入 `verification_joining` 标记（默认 TTL 3s），新增 `VerificationGuardMiddleware` 对新发群消息查此标记，命中即删除消息并阻断后续处理
- **策略**：对所有入群者统一适用，仅靠 TTL 过期；**只删消息不封禁**，避免误伤入群即发言的少数正常用户；Redis 查询失败 fail-open（WARNING 日志，不上报 Sentry）
- **注册顺序**：`WhitelistMiddleware` 之后、`CurfewMiddleware` 之前，仅拦截 message
- 新增配置项 `verification_joining_ttl`（`.env.example` 同步补充）

#### 群组消息用户名称脱敏显示 🔒
- **问题**：spammer 可将广告塞进用户显示名或 @username，bot 发往群组的含用户名消息原样展示，等于替垃圾信息二次曝光
- **新增 `mask_user_name`**：对用户名做星号遮盖，保留首尾各 1 个字符，中间替换为 `*`，短名按比例降级保证至少 1 个 `*`
- **统一应用**：所有群组用户提及走 `format_user_mention` / `masked_mention_html`（先脱敏后 HTML 转义），覆盖欢迎消息、CAS 群组通知等
- **举报处理者（管理员）名称不脱敏**，仅做 HTML 转义

### Bug 修复

#### `/reports` 列表 HTML 解析崩溃 🐛
- **问题**：`cmd_reports` 末尾提示语的字面 `<ID>` 在全局 `ParseMode.HTML` 下被当作未知标签解析，列表发送抛异常，管理员只看到兜底的「获取举报列表失败」
- **修复**：将 HTML 输出中未转义的字面尖括号占位符转为 HTML 实体（moderation 的 `<ID>`、admin 的 `/settimeout <秒数>`、curfew 的 `/curfew <开始时间> <结束时间>`）

#### CAS 群组通知优化
- CAS 中间件群组通知原仅显示数字 ID，改为脱敏用户名（`masked_mention_html`），与其他群组通知一致
- 移除 CAS 封禁通知中的违规次数显示（用户加入 / 消息拦截两处）及对应日志字段；审计日志 `offenses` 字段保留用于内部分析

### 文档更新
- README.md 拆分 release/tag 徽章，补充 CAS / 用户状态 / 宵禁功能说明
- `.env.example` 补充入群短窗口配置项 `verification_joining_ttl`

### 代码质量
- ✅ Sentry release 标识改为动态派生（`get_app_version`），跟随 `pyproject.toml` 版本号自动同步，消除此前硬编码 `1.3.0` 长期滞后的隐患
- ✅ 新增 `tests/test_verification_guard.py`（入群短窗口防护）
- ✅ 补充用户名脱敏、CAS 通知文案、占位符转义、版本号读取等单元测试

## [1.5.4] - 2026-07-09

### Bug 修复

#### 特殊发送者在 CAS / 状态检测前优先跳过 🐛
- **问题**：Telegram 系统服务账号 `777000`（关联频道同步转发 / 服务通知）在 CAS 中间件层缺少短路，导致每次同步消息都触发 CAS API 请求，失败时经 3 次指数退避重试后降级放行，造成无谓的网络请求、日志噪声与约 3.5s 处理延迟
  - 根因：`777000` 的跳过逻辑仅存在于 antispam handler 层，而 `CASCheckMiddleware` 在 handler 之前执行，handler 层的跳过为时已晚
- **修复**：在 `CASCheckMiddleware` 的 admin 检查前统一短路特殊发送者，CAS 与用户状态检查同时跳过
- **新增 `should_skip_sender()` 统一判断**（`core/utils.py`）：覆盖 Telegram 系统服务账号（`TELEGRAM_SERVICE_IDS`，含 `777000`）与 Bot 自身（防消息回环自检）
  - antispam 两处硬编码 `== 777000`（频道马甲检测、非文本活跃度检查）统一改用该函数，顺带覆盖 Bot 自身
  - `verification.py` 的入群 / 加入请求流程不受影响：`777000` 不会以新成员或申请者身份出现

### 代码质量
- ✅ `should_skip_sender` 签名收紧为 `(user_id: int, bot_id: int)`，与文件内 `validate_user_id` 风格一致
- ✅ codex read-only 审查通过（确认短路位置正确、无遗漏调用路径）
- ✅ Mypy 类型检查通过
- ✅ Ruff 代码检查通过

## [1.5.3] - 2026-07-02

### 行为优化

#### 活跃度非文本拦截收窄至「从未发言」用户 🎯
- **问题**：activity 启用时，所有活跃度为 0 的成员（含因日衰减归零的老用户）发非文本消息都会被删除，误伤长期潜水但曾活跃的真实用户
- **改进**：将拦截范围收窄为仅命中「从未发言」的成员；曾发言的老用户活跃度不再衰减到 0
  - 核心洞察：Redis 中「有 activity key」即代表曾发言——衰减下限只抬高这部分用户，无 key 的从未发言者保持 0 仍被拦截（含「入群后长期潜水再冒泡」的小号）
- **新增配置 `activity_decay_floor`**（默认 1，`ge=0`，设为 0 可回退旧行为）
- **衰减逻辑精确化**（`get_activity`）：
  - 仅当 `stored > activity_decay_floor` 时才衰减，结果 `max(stored - days, floor)` 保底
  - `stored <= floor`（含 0）不衰减、保持原值，避免活跃度 0 被反向「抬升」
  - 防御性读取分支仅用 `max(*, 0)` 防负数，不参与 floor 抬升
- **修复 `record_non_text_message`**：不再为从未发言用户创建 `activity=0` 键，避免其被衰减下限误判为「曾发言」而绕过拦截
- **文案同步**：更新 `/activity` 面板、`groupset` 菜单共 3 处活跃度规则说明

### 代码质量
- ✅ 新增 `tests/test_activity.py`（12 个用例，activity 服务覆盖率 0% → 57%+）
- ✅ codex 两轮代码审查通过
- ✅ Mypy 类型检查通过（无新增错误）
- ✅ Ruff 代码检查通过

## [1.5.2] - 2026-07-01

### 性能优化

#### 规则引擎关键词预筛门禁 ⚡
- **消除前瞻正则二次回溯**：为「多关键词 AND」型规则引入关键词预筛门禁，解决 `(?=.*X)` 前瞻在长文本上退化为 O(N²) 回溯的性能问题
  - 新增 `SpamRule.prefilter` 字段（外层组间 AND、内层组内 OR），默认空，未配置规则行为不变
  - `check()` 在执行正则前先做 O(N) 子串预判，未全组命中则跳过昂贵的正则匹配
  - 预筛作为原正则的**必要条件**：命中正则的文本必然通过预筛，判定语义完全不变
- **配置三条热点规则预筛**：`crypto_multi_keyword`、`adult_chat_service`、`recruit_quick_money`
  - 预筛关键词覆盖正则字符类的全部简繁体展开，确保必要条件成立
- **效果**：正常长消息（250 字）单条耗时 ~1196µs → ~99µs（约 12 倍提升）

### 新增功能

#### 拉人头暴富招募诈骗检测规则 🛡️
- **新增 `recruit_quick_money` 规则**：固化日志中高频出现的招募诈骗模板（如「带两个缺钱的兄弟…一天保你一万打底，进群找王哥」）
  - 采用「带 + 缺钱 + 兄弟 + (打底|进群)」多特征前瞻组合，无视语序、兼容简繁体
  - 风险等级 high，在规则引擎 Stage 1 直接拦截，避免走到验证超时

### 代码质量
- ✅ 新增 3 个预筛门禁单元测试（拦截 / 语义等价 / 默认行为）
- ✅ 20 万条随机模糊测试与纯正则判定零不一致
- ✅ Mypy 类型检查通过
- ✅ Ruff 代码检查通过

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
