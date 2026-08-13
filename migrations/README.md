# 数据库迁移（Alembic）

本项目使用 [Alembic](https://alembic.sqlalchemy.org/) 管理 PostgreSQL schema 迁移。

## 配置

- `alembic.ini`（项目根目录）：Alembic 主配置，`sqlalchemy.url` 留空，由 `migrations/env.py` 从 `settings.database_url` 注入（避免密码在 ConfigParser 的 `%` 插值中出错）
- `migrations/env.py`：异步迁移环境（`create_async_engine` + `NullPool`，显式 import 全部模型，含 Report）
- `migrations/versions/`：迁移脚本目录
- `migrations/legacy_sql/`：已废弃的手写 SQL 迁移归档（001-007，其 DDL 已并入 baseline，仅作历史留存）

## 常用命令

迁移通过 Docker 容器执行（无需 bot 在运行）：

```bash
# 应用所有待执行迁移到最新
make db-migrate

# 生成新迁移（修改 src/models/*.py 后）
make db-revision M="添加 xxx 字段"

# 回滚最近一个迁移
make db-down

# 查看当前版本 / 迁移历史
docker-compose run --rm bot alembic current
docker-compose run --rm bot alembic history
```

## 容器启动自动迁移

`docker-entrypoint.sh` 在启动 Bot 主进程前自动执行 `alembic upgrade head`，并用 PostgreSQL advisory lock 防止与手动迁移命令并发。迁移失败则阻止 Bot 启动。

手动执行 `make db-migrate` / `db-revision` / `db-down` 时，entrypoint 检测到首参为 `alembic` 直接透传，不重复执行自动迁移。

## 工作流

### 新增 / 修改表结构

1. 修改 `src/models/*.py` 中的模型定义
2. `make db-revision M="描述变更"` 生成迁移脚本（autogenerate）
3. **审查生成的迁移**：autogenerate 可能漏检 rename（会识别成删列 + 加列）、枚举类型、约束名；必要时手动调整
4. `make db-migrate` 应用迁移
5. `make check` 验证

### baseline（初始迁移）

`20260813_c3d35c9d5221_initial_schema.py` 是初始 baseline，涵盖全部 6 张表、索引、外键与 server_default。

- **全新数据库**：`alembic upgrade head` 直接建表
- **已有生产库**（schema 与 baseline 一致）：`alembic stamp c3d35c9d5221` 标记到位，不执行 DDL
- baseline 的 `downgrade()` 故意留空——回滚 baseline 等于删表，灾难恢复请用 `scripts/backup.py`（pg_dump）或 `docker-compose down -v` 重建数据卷

## 注意事项

1. **autogenerate 不是银弹**：rename column 会被误判为删旧列 + 加新列（丢数据），需手动改为 `op.alter_column`；server_default / 约束名 / 注释变更可能漏检
2. **server_default 契约**：模型中 `default=` 是 Python 端默认值，`server_default=` 才写入 DB DDL。NOT NULL 列若需支持原生 SQL INSERT，应配 `server_default`
3. **不要在生产执行 `alembic downgrade base`**：会删除全部表。灾难回滚用 pg_dump
4. **`schema_migrations` 表**：旧自研脚本的记录表，迁移到 Alembic 后保留不删（仅作历史），新迁移走 `alembic_version` 表
5. **索引命名**：显式 `Index("idx_xxx", ...)` 保持稳定命名，便于 autogenerate diff；主键列不要再加冗余索引（PG 主键自带索引）
