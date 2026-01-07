# 数据库迁移指南

本目录包含数据库 schema 的版本化迁移文件。

## 迁移文件命名规则

迁移文件按顺序编号，格式为：`{编号}_{描述}.sql`

例如：
- `001_add_activity_enabled.sql` - 添加活跃度系统开关字段
- `002_add_some_feature.sql` - 添加某个功能

## 快速开始

### 1. 查看迁移状态

```bash
python scripts/migrate_schema.py --list
```

### 2. 应用所有待处理的迁移

```bash
python scripts/migrate_schema.py --run
```

### 3. Docker 环境中运行

```bash
# 查看状态
docker exec tg-guard-bot python scripts/migrate_schema.py --list

# 应用迁移
docker exec tg-guard-bot python scripts/migrate_schema.py --run
```

## 生产环境部署

### 方式一：使用迁移脚本（推荐）

```bash
# 1. 进入容器
docker exec -it tg-guard-bot bash

# 2. 运行迁移
python scripts/migrate_schema.py --run
```

### 方式二：直接执行 SQL

如果你更喜欢手动控制，可以直接在 PostgreSQL 中执行 SQL 文件：

```bash
# 方式 A：使用 psql
docker exec -i tg-guard-postgres psql -U postgres -d tg_guard < migrations/001_add_activity_enabled.sql

# 方式 B：进入 psql 交互式环境
docker exec -it tg-guard-postgres psql -U postgres -d tg_guard
\i /path/to/migrations/001_add_activity_enabled.sql
```

## 迁移文件清单

| 文件 | 版本 | 描述 | 应用日期 |
|------|------|------|---------|
| 001_add_activity_enabled.sql | 001 | 添加群组活跃度系统开关字段 | - |

## 迁移跟踪

系统使用 `schema_migrations` 表记录已应用的迁移：

```sql
-- 查看已应用的迁移
SELECT * FROM schema_migrations ORDER BY version;

-- 手动标记迁移为已应用（仅在必要时使用）
INSERT INTO schema_migrations (version, description)
VALUES ('001_add_activity_enabled', 'Manually marked as applied');
```

## 注意事项

1. **幂等性**：所有迁移都应该是幂等的（使用 `IF NOT EXISTS`、`IF EXISTS` 等）
2. **顺序执行**：迁移按文件名顺序执行，请确保编号连续
3. **备份数据**：生产环境应用迁移前务必备份数据库
4. **测试环境验证**：先在测试环境验证迁移无误后再应用到生产环境

## 创建新迁移

1. 在 `migrations/` 目录创建新的 SQL 文件
2. 使用递增的编号（例如：`002_xxx.sql`）
3. 编写幂等的 SQL 语句
4. 在测试环境验证
5. 提交到版本控制

## 故障排除

### 迁移失败回滚

如果迁移失败，系统会自动回滚事务。你可以：

1. 修复迁移文件中的错误
2. 重新运行 `python scripts/migrate_schema.py --run`

### 手动回滚迁移

如果需要回滚已应用的迁移：

```sql
-- 1. 手动执行回滚 SQL
-- 2. 从迁移记录表删除该版本
DELETE FROM schema_migrations WHERE version = '001_add_activity_enabled';
```

## 与现有 migrate.py 的区别

- **migrate.py**：使用 SQLAlchemy 的 `create_all()`，适用于全新数据库初始化
- **migrate_schema.py**：版本化迁移管理，适用于生产环境的 schema 更新

建议：
- 新部署：使用 `migrate.py`
- 升级现有环境：使用 `migrate_schema.py`
