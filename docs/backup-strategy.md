# 自动备份策略文档

## 概述

本项目实现了 PostgreSQL + Redis 的自动备份机制，采用 **GFS (Grandfather-Father-Son) 轮转策略**，确保数据安全且节省存储空间。

## 备份策略

### GFS 轮转策略

| 类型 | 保留时间 | 保留数量 | 备份时机 | 目录 |
|------|---------|----------|---------|------|
| **Daily** | 7 天 | 最多 7 个 | 每日凌晨 3:00 | `backups/daily/` |
| **Weekly** | 28 天 (4 周) | 最多 4 个 | 每周日凌晨 3:00 | `backups/weekly/` |
| **Monthly** | 180 天 (6 个月) | 最多 6 个 | 每月第一个周日凌晨 3:00 | `backups/monthly/` |

### 轮转流程

```
每日凌晨 3:00 执行：
├─ 1. 创建 PostgreSQL daily 备份 → backups/daily/postgres_YYYYMMDD.sql
├─ 2. 创建 Redis daily 备份 → backups/daily/redis_YYYYMMDD.rdb
├─ 3. 如果是周日 → 复制当日备份到 weekly/
│   ├─ backups/daily/postgres_20260105.sql
│   └─ backups/weekly/postgres_20260105_weekly.sql
├─ 4. 如果是每月第一个周日 → 复制当日备份到 monthly/
│   ├─ backups/daily/postgres_20260105.sql
│   └─ backups/monthly/postgres_20260105_monthly.sql
└─ 5. 清理过期备份
    ├─ daily/: 删除超过 7 天的备份
    ├─ weekly/: 删除超过 28 天的备份
    └─ monthly/: 删除超过 180 天的备份
```

## 目录结构

```
backups/
├── daily/                    # 每日备份（7 天）
│   ├── postgres_20260109.sql
│   ├── postgres_20260108.sql
│   ├── postgres_20260107.sql
│   ├── redis_20260109.rdb
│   ├── redis_20260108.rdb
│   └── redis_20260107.rdb
├── weekly/                   # 每周备份（4 周，周日）
│   ├── postgres_20260105_weekly.sql
│   ├── postgres_20251229_weekly.sql
│   ├── redis_20260105_weekly.rdb
│   └── redis_20251229_weekly.rdb
└── monthly/                  # 月备份（6 个月，每月第一个周日）
    ├── postgres_20260105_monthly.sql
    ├── postgres_20251201_monthly.sql
    ├── redis_20260105_monthly.rdb
    └── redis_20251201_monthly.rdb
```

## 使用方法

### 手动备份

```bash
# 完整备份（PostgreSQL + Redis + 轮转）
make backup

# 仅备份 PostgreSQL
make backup-postgres

# 仅备份 Redis
make backup-redis

# 清理过期备份
make backup-cleanup
```

### 查看备份

```bash
# 列出所有备份
make backup-list

# 输出示例：
# === Daily 备份（保留 7 天，共 6 个）===
# 1. postgres_20260109.sql - 2.15 MB - 1小时前 (2026-01-09 02:00:00)
# 2. redis_20260109.rdb - 0.05 MB - 1小时前 (2026-01-09 02:00:00)
# ...
# === Weekly 备份（保留 4 周，共 4 个）===
# 1. postgres_20260105_weekly.sql - 2.10 MB - 4天前 (2026-01-05 03:00:00)
# ...
# === Monthly 备份（保留 6 个月，共 3 个）===
# 1. postgres_20260105_monthly.sql - 2.05 MB - 4天前 (2026-01-05 03:00:00)
# ...
```

### 恢复备份

#### 恢复 PostgreSQL

```bash
# 从 daily 备份恢复
make backup-restore-postgres FILE=backups/daily/postgres_20260109.sql

# 从 weekly 备份恢复
make backup-restore-postgres FILE=backups/weekly/postgres_20260105_weekly.sql

# 从 monthly 备份恢复
make backup-restore-postgres FILE=backups/monthly/postgres_20260105_monthly.sql
```

#### 恢复 Redis

```bash
# 恢复 Redis（会自动重启容器）
make backup-restore-redis FILE=backups/daily/redis_20260109.rdb
make backup-restore-redis FILE=backups/weekly/redis_20260105_weekly.rdb
make backup-restore-redis FILE=backups/monthly/redis_20260105_monthly.rdb

# 注意：Redis 恢复会短暂中断服务（约 1-2 秒）
```

### 配置自动备份

#### 方式一：使用脚本（推荐）

```bash
# 自动配置 crontab
make backup-setup-cron

# 验证配置
crontab -l | grep backup
# 输出：0 3 * * * cd /path/to/tg-guard-bot && make backup >> /path/to/logs/backup.log 2>&1
```

#### 方式二：手动配置 crontab

```bash
# 编辑 crontab
crontab -e

# 添加以下行：
0 3 * * * cd /path/to/tg-guard-bot && make backup >> /path/to/tg-guard-bot/logs/backup.log 2>&1
```

#### 删除定时任务

```bash
crontab -l | grep -v "tg-guard-bot" | crontab -
```

## 备份原理

### PostgreSQL 备份

使用 `pg_dump` 生成纯文本 SQL 格式备份：

```bash
pg_dump -h localhost -p 5432 -U postgres -d tg_guard -F p -f backup.sql
```

**优点**：
- 纯文本格式，易于查看和编辑
- 可跨版本恢复
- 压缩率高（可配合 gzip）

**缺点**：
- 大数据库备份较慢（5 分钟超时）
- 占用空间较大（约 2-5MB）

### Redis 备份

通过 Docker 容器触发 `BGSAVE` 并复制 `dump.rdb` 文件：

```bash
# 1. 触发后台保存
docker exec tg-guard-redis redis-cli -a PASSWORD BGSAVE

# 2. 等待 BGSAVE 完成
docker exec tg-guard-redis redis-cli -a PASSWORD LASTSAVE

# 3. 复制 RDB 文件
docker cp tg-guard-redis:/data/dump.rdb backups/daily/redis_20260109.rdb
```

**优点**：
- 快速（秒级完成）
- 占用空间小（通常 < 1MB）
- 二进制格式，恢复速度快

**缺点**：
- 无法跨 Redis 大版本恢复
- 二进制格式不可读

## 存储空间估算

### 单次备份大小

| 数据源 | 估算大小 | 说明 |
|--------|---------|------|
| PostgreSQL | 2-5 MB | 取决于群组数量和用户警告记录 |
| Redis | 0.05-0.5 MB | 取决于验证队列和缓存数据 |
| **总计** | **~3-6 MB** | 每次备份约 3-6 MB |

### 总存储空间

```
Daily (7 天) × 2 份（PG + Redis） = 14 个文件 × 3MB ≈ 42 MB
Weekly (4 周) × 2 份（PG + Redis） = 8 个文件 × 3MB ≈ 24 MB
Monthly (6 个月) × 2 份（PG + Redis） = 12 个文件 × 3MB ≈ 36 MB
-----------------------------------------------------------
总计: 约 102 MB (建议预留 150 MB)
```

## 监控与日志

### 备份日志

所有备份操作记录在：
```bash
logs/backup.log
```

**日志示例**：
```
2026-01-09 03:00:01 | INFO | === 开始执行自动备份流程 ===
2026-01-09 03:00:02 | INFO | 开始备份 PostgreSQL 到: backups/daily/postgres_20260109.sql
2026-01-09 03:00:05 | INFO | ✅ PostgreSQL 备份成功: postgres_20260109.sql (2.15 MB)
2026-01-09 03:00:06 | INFO | 开始备份 Redis 到: backups/daily/redis_20260109.rdb
2026-01-09 03:00:08 | INFO | ✅ Redis 备份成功: redis_20260109.rdb (0.05 MB)
2026-01-09 03:00:08 | INFO | 开始清理过期备份...
2026-01-09 03:00:08 | INFO | 删除过期 daily 备份: postgres_20260101.sql
2026-01-09 03:00:08 | INFO | ✅ 清理完成，删除 2 个 daily 备份，1 个 weekly 备份
2026-01-09 03:00:08 | INFO | === 备份流程完成，耗时 7.23 秒 ===
2026-01-09 03:00:08 | INFO | ✅ 备份成功
```

### 查看备份日志

```bash
# 查看最近的备份日志
tail -f logs/backup.log

# 查看今天的备份记录
grep "$(date +%Y-%m-%d)" logs/backup.log

# 检查备份是否成功
grep "✅ 备份成功" logs/backup.log | tail -1
```

### 监控告警

建议配置以下监控指标：

| 指标 | 监控方式 | 告警条件 |
|------|---------|---------|
| 备份成功率 | 检查 `✅ 备份成功` 日志 | 24小时内无成功记录 |
| 备份文件大小 | 检查备份文件 | 文件大小突然变化 > 50% |
| 磁盘空间 | `df -h backups/` | 剩余空间 < 200MB |
| 备份耗时 | 解析日志耗时 | 单次备份 > 5 分钟 |

## 灾难恢复流程

### 场景 1：单独恢复 PostgreSQL

**适用情况**：数据库损坏，但 Redis 正常

```bash
# 1. 停止 bot 服务
docker-compose stop bot

# 2. 恢复最近的备份
make backup-restore-postgres FILE=backups/daily/postgres_20260109.sql

# 3. 重启 bot 服务
docker-compose start bot

# 4. 验证数据
make db-shell
# 在 psql 中执行：SELECT COUNT(*) FROM groups;
```

### 场景 2：单独恢复 Redis

**适用情况**：Redis 数据丢失，但数据库正常

```bash
# 恢复会自动停止/启动 Redis 容器
make backup-restore-redis FILE=backups/daily/redis_20260109.rdb

# 验证数据
docker-compose exec redis redis-cli -a $REDIS_PASSWORD DBSIZE
```

### 场景 3：完整灾难恢复

**适用情况**：服务器故障，需要完全恢复

```bash
# 1. 部署新环境
git clone https://github.com/your-repo/tg-guard-bot.git
cd tg-guard-bot
cp .env.example .env
# 编辑 .env 配置

# 2. 启动服务
docker-compose up -d

# 3. 恢复 PostgreSQL
make backup-restore-postgres FILE=backups/daily/postgres_20260109.sql

# 4. 恢复 Redis
make backup-restore-redis FILE=backups/daily/redis_20260109.rdb

# 5. 重启所有服务
docker-compose restart

# 6. 验证服务
make dev-logs
```

## 常见问题

### Q: 备份失败，提示 "pg_dump: command not found"

**A**: 需要在 Docker 容器内执行备份脚本：

```bash
# 错误方式（宿主机没有 pg_dump）
python scripts/backup.py

# 正确方式（容器内有 pg_dump）
make backup
```

### Q: Redis 备份时提示 "BGSAVE already in progress"

**A**: Redis 正在执行上一次 BGSAVE，等待完成后再试：

```bash
# 检查 BGSAVE 状态
docker-compose exec redis redis-cli -a $REDIS_PASSWORD INFO persistence | grep rdb_bgsave_in_progress

# 如果为 1，等待完成；如果为 0，可以重新备份
```

### Q: 周日没有自动创建 weekly 备份

**A**: 检查以下几点：

1. 确认 crontab 已正确配置：
   ```bash
   crontab -l | grep backup
   ```

2. 检查备份日志：
   ```bash
   grep "weekly" logs/backup.log
   ```

3. 手动测试 weekly 提升（仅限周日）：
   ```bash
   make backup
   ```

### Q: 恢复备份后数据不一致

**A**: 可能原因：

1. **时间点不一致**：PostgreSQL 和 Redis 备份时间不同
   - 解决方案：同时恢复同一时刻的备份

2. **缓存未清理**：Redis 缓存了旧的数据库查询结果
   - 解决方案：清空 Redis 缓存
     ```bash
     docker-compose exec redis redis-cli -a $REDIS_PASSWORD FLUSHDB
     docker-compose restart bot
     ```

### Q: 如何异地备份？

**A**: 推荐方案：

1. **方案 A：rsync 定时同步到远程服务器**
   ```bash
   # 添加到 crontab
   30 3 * * * rsync -avz /path/to/backups/ user@remote:/path/to/backups/
   ```

2. **方案 B：上传到云存储（S3/OSS）**
   ```bash
   # 使用 aws-cli
   aws s3 sync /path/to/backups/ s3://bucket-name/backups/
   ```

3. **方案 C：使用 rclone 同步多个云盘**
   ```bash
   rclone sync /path/to/backups/ remote:backups/
   ```

## 最佳实践

1. **定期测试恢复**
   - 每月至少测试一次完整恢复流程
   - 记录恢复时间和问题

2. **监控备份日志**
   - 配置日志告警（邮件/企业微信/钉钉）
   - 关注 "❌ 备份失败" 日志

3. **异地存储**
   - 重要数据必须异地备份
   - 推荐使用云存储（S3/OSS/COS）

4. **文档化**
   - 记录恢复流程和注意事项
   - 更新联系人和权限信息

5. **定期清理**
   - 手动检查备份目录大小
   - 删除不必要的临时文件

6. **安全性**
   - 备份文件包含敏感数据，确保权限正确
   - 考虑加密敏感备份（如 GPG）

## 技术参考

- **PostgreSQL 备份**: https://www.postgresql.org/docs/16/backup-dump.html
- **Redis 持久化**: https://redis.io/docs/management/persistence/
- **Crontab 语法**: https://crontab.guru/
- **GFS 备份策略**: https://en.wikipedia.org/wiki/Backup_rotation_scheme

---

**最后更新**: 2026-01-09
**版本**: v1.0
**维护者**: tg-guard-bot 团队
