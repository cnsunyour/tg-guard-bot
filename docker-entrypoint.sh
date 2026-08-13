#!/bin/sh
# ============================================================================
# Docker 容器入口脚本
#
# 职责：在启动 Bot 主进程前，先执行 Alembic 数据库迁移（仅自动启动模式）。
# - 首参为 alembic（手动迁移命令，如 make db-migrate / db-revision / db-down）
#   → 直接透传，不重复迁移（避免与 ENTRYPOINT 内的自动迁移双重执行）
# - 首参为 python（默认 CMD=python -m src.main）→ 先迁移再启动
# - 迁移失败则非 0 退出，阻止 Bot 启动
# ============================================================================

# 手动 alembic 子命令：直接透传，不自动迁移
case "${1:-}" in
    alembic)
        exec "$@"
        ;;
esac

# 默认应用启动：先迁移再启动
echo "[entrypoint] 开始数据库迁移..."

# 使用 PG 会话级 advisory lock 防止「容器启动自动迁移」与并发迁移冲突。
# asyncpg 已随应用安装，无需容器内额外装 psql。
if ! python - <<'PY'
import asyncio
import contextlib
import sys

import asyncpg

from src.core.config import settings

# 固定的 advisory lock key（任意 bigint，全局唯一即可）
ADVISORY_LOCK_KEY = 814742921


async def run_migration() -> int:
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        timeout=30,
    )
    try:
        print("[entrypoint] 等待数据库迁移锁...", flush=True)
        await conn.execute("SELECT pg_advisory_lock($1::bigint)", ADVISORY_LOCK_KEY)
        print("[entrypoint] 已获取迁移锁", flush=True)

        # 旧库 bootstrap 检测：若已有业务表但无 alembic_version（旧自研迁移遗留），
        # 直接 upgrade head 会因 DuplicateTable 失败。先 stamp baseline 标记到位，
        # 再 upgrade head 应用后续增量迁移。全新库无表则正常 upgrade 建表。
        has_tables = await conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name NOT IN ('alembic_version', 'schema_migrations')"
        )
        has_alembic_version = await conn.fetchval(
            "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        )
        if has_tables > 0 and not has_alembic_version:
            print(
                "[entrypoint] 检测到旧库（有表无 alembic_version），"
                "执行 stamp baseline 标记已有 schema",
                flush=True,
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "alembic", "stamp", "c3d35c9d5221"
            )
            rc = await proc.wait()
            if rc != 0:
                print("[entrypoint] stamp baseline 失败", file=sys.stderr, flush=True)
                return rc

        print("[entrypoint] 执行 alembic upgrade head", flush=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", "upgrade", "head"
        )
        return await proc.wait()
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SELECT pg_advisory_unlock($1::bigint)", ADVISORY_LOCK_KEY)
        with contextlib.suppress(Exception):
            await conn.close()


raise SystemExit(asyncio.run(run_migration()))
PY
then
    echo "[entrypoint] 数据库迁移失败，停止 Bot 启动。" >&2
    exit 1
fi

echo "[entrypoint] 数据库迁移成功，启动 Bot..."
exec "$@"
