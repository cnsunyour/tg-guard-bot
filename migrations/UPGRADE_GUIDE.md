# 生产环境升级指南 - 活跃度系统

本文档说明如何将现有生产环境升级以支持群组活跃度系统开关功能。

## 变更概述

**版本**: v1.1 - 活跃度系统群组开关
**发布日期**: 2026-01-07
**影响范围**: 数据库 schema

### 主要变更

1. **数据库变更**：在 `groups` 表添加 `activity_enabled` 字段
2. **新增功能**：管理员可通过 `/activity` 命令控制群组的活跃度系统开关
3. **默认行为**：新群组和现有群组默认启用活跃度系统

---

## 升级前准备

### 1. 备份数据库

```bash
# 备份 PostgreSQL 数据库
docker exec tg-guard-postgres pg_dump -U postgres tg_guard > backup_$(date +%Y%m%d_%H%M%S).sql
```

### 2. 检查当前版本

```bash
# 查看当前数据库表结构
docker exec tg-guard-postgres psql -U postgres -d tg_guard -c "\d groups"
```

---

## 升级步骤

### 方式一：自动迁移（推荐）

#### 步骤 1：更新代码

```bash
# 拉取最新代码
git pull origin main

# 或者如果在 dev 分支
git pull origin dev
```

#### 步骤 2：重新构建镜像

```bash
# 重新构建 Docker 镜像（包含新的 migrations 目录）
make prod-build

# 或者手动构建
docker-compose build bot
```

#### 步骤 3：运行迁移

```bash
# 运行数据库迁移（在启动新容器前）
docker-compose run --rm bot python scripts/migrate_schema.py --run
```

#### 步骤 4：重启服务

```bash
# 重启 Bot 服务
docker-compose up -d bot
```

### 方式二：手动 SQL（仅限熟悉 SQL 的管理员）

如果你更喜欢直接执行 SQL：

```bash
# 连接到 PostgreSQL 容器
docker exec -it tg-guard-postgres psql -U postgres -d tg_guard

# 执行以下 SQL：
ALTER TABLE groups
ADD COLUMN IF NOT EXISTS activity_enabled BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN groups.activity_enabled IS '是否启用活跃度系统';

-- 验证
\d groups
```

---

## 升级后验证

### 1. 检查字段是否添加

```bash
docker exec tg-guard-postgres psql -U postgres -d tg_guard -c "\d groups" | grep activity
```

预期输出应包含：
```
activity_enabled | boolean | | not null | true
```

### 2. 测试新功能

1. 在任意测试群组发送 `/activity` 命令
2. 验证是否显示活跃度系统控制面板
3. 点击启用/禁用按钮测试功能
4. 发送 `/verifyconfig` 验证配置显示是否正确

### 3. 检查日志

```bash
# 查看 Bot 日志
docker logs tg-guard-bot --tail 50

# 确认无错误
```

---

## 回滚方案

如果升级后出现问题，可以按以下步骤回滚：

### 步骤 1：停止服务

```bash
docker-compose stop bot
```

### 步骤 2：恢复数据库（如果需要）

```bash
# 恢复备份
docker exec -i tg-guard-postgres psql -U postgres -d tg_guard < backup_YYYYMMDD_HHMMSS.sql
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

## 迁移工具使用说明

### 查看迁移状态

```bash
docker exec tg-guard-bot python scripts/migrate_schema.py --list
```

### 手动应用单个迁移

```bash
docker exec -i tg-guard-postgres psql -U postgres -d tg_guard < migrations/001_add_activity_enabled.sql
```

---

## 常见问题

### Q1: 字段已存在错误

**问题**: 执行迁移时提示字段已存在
**原因**: 可能之前已手动添加过该字段
**解决**:

```sql
-- 检查字段是否存在
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'groups' AND column_name = 'activity_enabled';

-- 如果字段已存在但类型不对，可以删除后重新添加
ALTER TABLE groups DROP COLUMN IF EXISTS activity_enabled;
ALTER TABLE groups ADD COLUMN activity_enabled BOOLEAN NOT NULL DEFAULT TRUE;
```

### Q2: 迁移记录不一致

**问题**: 迁移脚本显示未应用，但字段已存在
**解决**: 手动标记为已应用

```sql
-- 创建迁移记录表（如果不存在）
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT NOW(),
    description TEXT
);

-- 手动标记迁移为已应用
INSERT INTO schema_migrations (version, description)
VALUES ('001_add_activity_enabled', 'Manually applied')
ON CONFLICT DO NOTHING;
```

### Q3: 权限问题

**问题**: 用户权限不足，无法修改表结构
**解决**: 使用 postgres 用户执行，或授予相应权限

```sql
GRANT ALL PRIVILEGES ON TABLE groups TO your_user;
GRANT ALL PRIVILEGES ON TABLE schema_migrations TO your_user;
```

---

## 技术细节

### 数据库变更详情

```sql
-- 变更内容
ALTER TABLE groups ADD COLUMN activity_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- 字段说明
-- activity_enabled: 群组是否启用活跃度系统
--   - TRUE (默认): 启用
--   - FALSE: 禁用

-- 生效逻辑
-- 活跃度系统生效 = 全局开关 (settings.activity_enabled) AND 群组开关 (group.activity_enabled)
```

### 影响范围

- **现有用户**: 无影响，活跃度数据保留
- **现有群组**: 默认启用活跃度系统
- **新群组**: 默认启用活跃度系统

---

## 联系支持

如遇问题，请：

1. 检查日志：`docker logs tg-guard-bot`
2. 查看迁移状态：`docker exec tg-guard-bot python scripts/migrate_schema.py --list`
3. 提交 Issue：https://github.com/your-repo/tg-guard-bot/issues
