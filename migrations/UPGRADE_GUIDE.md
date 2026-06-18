# 生产环境升级指南 - 活跃度系统

本文档说明如何将现有生产环境升级以支持群组活跃度系统开关功能。

## 变更概述

**版本**: v1.4.3 - 活跃度系统简化
**发布日期**: 2026-06-15
**影响范围**: 配置文件（无需数据库迁移）

### 主要变更

1. **配置简化**：删除全局开关 `ACTIVITY_ENABLED`，只保留群组开关
2. **功能增强**：活跃度记录、置信度修正、检测豁免功能始终工作
3. **行为变更**：群组开关只控制"是否限制活跃度 ≤ 0 的用户发送非文本消息"
4. **向后兼容**：数据库结构无变化，群组配置完全兼容

---

## 升级前准备

### 1. 备份配置文件

```bash
# 备份 .env 配置
cp .env .env.backup_$(date +%Y%m%d_%H%M%S)
```

### 2. 检查当前版本

```bash
# 查看当前配置
grep ACTIVITY .env
```

---

## 升级步骤

### 步骤 1：更新代码

```bash
# 拉取最新代码
git pull origin main

# 或者如果在 dev 分支
git pull origin dev
```

### 步骤 2：更新配置文件

```bash
# 编辑 .env 文件
nano .env

# 删除以下行（如果存在）：
# ACTIVITY_ENABLED=true

# 保留以下配置：
# ACTIVITY_MAX_CONFIDENCE_REDUCTION=0.15
# ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD=0
```

### 步骤 3：重新构建并启动

```bash
# 重新构建 Docker 镜像
make prod-build

# 重启服务
make prod-restart
```

---

## 升级后验证

### 1. 检查服务启动

```bash
# 查看 Bot 日志
docker logs tg-guard-bot --tail 50

# 确认无错误（即使保留了 ACTIVITY_ENABLED 也不会报错）
```

### 2. 测试新功能

1. 在任意测试群组发送 `/activity` 命令
2. 验证新的说明文案（应显示"始终生效"的辅助功能）
3. 测试启用/禁用开关：
   - **启用时**：活跃度 ≤ 0 的用户无法发送非文本消息
   - **禁用时**：新用户可自由发送任何消息
4. 验证辅助功能始终工作：
   - 活跃度记录
   - 置信度修正
   - 检测豁免

### 3. 检查配置加载

```bash
# 确认配置正确加载
docker exec tg-guard-bot python -c "
from src.core.config import settings
print(f'ACTIVITY_MAX_CONFIDENCE_REDUCTION: {settings.activity_max_confidence_reduction}')
print(f'ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD: {settings.activity_skip_spam_check_threshold}')
print(f'Has activity_enabled: {hasattr(settings, \"activity_enabled\")}')
"
```

预期输出：
```
ACTIVITY_MAX_CONFIDENCE_REDUCTION: 0.15
ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD: 0
Has activity_enabled: False
```

---

## 回滚方案

如果升级后出现问题，可以按以下步骤回滚：

### 步骤 1：停止服务

```bash
docker-compose stop bot
```

### 步骤 2：恢复配置

```bash
# 恢复备份的配置文件
cp .env.backup_YYYYMMDD_HHMMSS .env
```

### 步骤 3：回滚代码

```bash
# 切换到上一个稳定版本
git checkout <上一个版本的 commit hash>

# 重新构建
make prod-build
```

### 步骤 4：启动旧版本

```bash
docker-compose up -d bot
```

---

## 行为变更详解

### 修改前（v1.4.2 及之前）

```
全局开关 OFF → 所有功能关闭（限制、记录、置信度修正、检测豁免）
全局开关 ON + 群组开关 OFF → 所有功能关闭
全局开关 ON + 群组开关 ON → 所有功能启用
```

### 修改后（v1.4.3+）

```
群组开关 ON → 限制活跃度 ≤ 0 的用户发送非文本消息
群组开关 OFF → 不限制非文本消息

活跃度记录：始终工作
置信度修正：始终工作
检测豁免：始终工作
宵禁门槛：始终工作
```

---

## 常见问题

### Q1: 保留了 ACTIVITY_ENABLED 会报错吗？

**答案**: 不会。即使 `.env` 中保留了 `ACTIVITY_ENABLED=true`，也不会报错，只是该配置不再被使用。

**建议**: 为了保持配置文件清洁，建议删除该行。

### Q2: 现有群组的行为会改变吗？

**答案**: 不会。群组配置 `activity_enabled` 保持不变：
- 之前启用的群组：仍然限制活跃度 ≤ 0 的用户
- 之前禁用的群组：仍然不限制

**新增**: 所有群组的辅助功能（置信度修正、检测豁免）现在始终工作。

### Q3: 活跃度数据会丢失吗？

**答案**: 不会。所有活跃度数据保留在 Redis 中，功能完全兼容。

### Q4: 宵禁模式还能用吗？

**答案**: 完全可以。宵禁模式下的活跃度门槛机制继续工作，不受此次变更影响。

---

## 技术细节

### 配置变更详情

```bash
# 删除的配置
ACTIVITY_ENABLED=true  # 全局开关，已删除

# 保留的配置
ACTIVITY_MAX_CONFIDENCE_REDUCTION=0.15   # 置信度修正最大降低值
ACTIVITY_SKIP_SPAM_CHECK_THRESHOLD=0     # 跳过检测阈值
```

### 代码变更详情

- **删除**: `src/core/config.py` 中的 `activity_enabled` 字段
- **修改**: `src/services/activity.py` - 方法签名更新
- **修改**: `src/bot/handlers/antispam.py` - 移除全局开关检查
- **修改**: `src/bot/handlers/admin.py` - 更新命令文案

### 影响范围

- **现有用户**: 无影响，活跃度数据保留
- **现有群组**: 行为不变，配置保持
- **新群组**: 默认启用活跃度限制
- **辅助功能**: 所有群组始终享受置信度修正和检测豁免

---

## 联系支持

如遇问题，请：

1. 检查日志：`docker logs tg-guard-bot`
2. 验证配置：参考上文"升级后验证"章节
3. 提交 Issue：https://github.com/cnsunyour/tg-guard-bot/issues
