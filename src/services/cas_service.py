"""CAS (Combot Anti-Spam) 本地快照服务"""

import asyncio
import contextlib
import csv
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from loguru import logger

from src.core.config import settings
from src.core.executor import run_in_executor


@dataclass
class CASCheckResult:
    """CAS 检查结果"""

    is_banned: bool
    user_id: int
    offenses: int = 0
    time_added: datetime | None = None
    error: str | None = None
    cached: bool = False


class CASService:
    """CAS (Combot Anti-Spam) 本地快照服务"""

    def __init__(self) -> None:
        self._snapshot_path = Path(settings.cas_export_path)
        self._snapshot: dict[int, tuple[int, int | None]] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._last_success_at: datetime | None = None
        self._record_count = 0

    async def start(self) -> None:
        """启动 CAS 快照服务"""
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        loaded = await self._load_snapshot()
        refreshed = False
        if not loaded:
            logger.warning("未找到可用的 CAS 本地快照，尝试立即下载")
            refreshed = await self._refresh_once(reason="bootstrap")
            if not refreshed:
                logger.warning("CAS 快照初始化失败，当前将降级放行")

        self._refresh_task = asyncio.create_task(
            self._refresh_loop(run_immediately=loaded and not refreshed),
            name="cas_refresh_loop",
        )

    async def close(self) -> None:
        """关闭后台刷新任务"""
        if self._refresh_task is None:
            return

        self._refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._refresh_task
        self._refresh_task = None

    async def check_user(self, user_id: int) -> CASCheckResult:
        """使用本地快照检查用户是否在 CAS 黑名单中"""
        result = self._lookup_local(user_id)
        if result is not None:
            return result

        return CASCheckResult(
            is_banned=False,
            user_id=user_id,
            error="snapshot_unavailable",
            cached=False,
        )

    async def _refresh_loop(self, *, run_immediately: bool) -> None:
        """后台周期刷新 CAS 快照"""
        if run_immediately:
            await self._refresh_once(reason="startup")

        while True:
            try:
                sleep_seconds = settings.cas_refresh_interval_seconds
                if self._snapshot is None:
                    sleep_seconds = min(60, sleep_seconds)

                await asyncio.sleep(sleep_seconds)
                await self._refresh_once(reason="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"CAS 快照后台刷新失败: {e}")

    async def _load_snapshot(self) -> bool:
        """加载本地快照文件"""
        if not self._snapshot_path.is_file():
            return False

        try:
            snapshot = await run_in_executor(self._parse_snapshot_file, self._snapshot_path)
        except Exception as e:
            logger.warning(f"CAS 本地快照加载失败 [path:{self._snapshot_path}]: {e}")
            return False

        self._snapshot = snapshot
        self._record_count = len(snapshot)

        with contextlib.suppress(OSError):
            self._last_success_at = datetime.fromtimestamp(
                self._snapshot_path.stat().st_mtime, tz=UTC
            )

        logger.info(
            f"CAS 本地快照加载成功 [path:{self._snapshot_path}] [records:{self._record_count}]"
        )
        self._warn_if_snapshot_stale()
        return True

    async def _refresh_once(self, *, reason: str) -> bool:
        """下载并原子刷新 CAS 快照"""
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._snapshot_path.with_suffix(f"{self._snapshot_path.suffix}.tmp")

        try:
            content = await self._download_snapshot_bytes()
            await run_in_executor(tmp_path.write_bytes, content)
            snapshot = await run_in_executor(self._parse_snapshot_file, tmp_path)
            await run_in_executor(os.replace, tmp_path, self._snapshot_path)

            self._snapshot = snapshot
            self._record_count = len(snapshot)
            self._last_success_at = datetime.now(UTC)

            logger.info(
                f"CAS 快照刷新成功 [reason:{reason}] [path:{self._snapshot_path}] "
                f"[records:{self._record_count}]"
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"CAS 快照刷新失败 [reason:{reason}]: {e}")
            self._warn_if_snapshot_stale()
            return False
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()

    async def _download_snapshot_bytes(self) -> bytes:
        """下载 CAS 导出文件"""
        timeout = httpx.Timeout(
            settings.cas_download_timeout,
            connect=min(10.0, float(settings.cas_download_timeout)),
        )

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=1, max_connections=2),
        ) as client:
            response = await client.get(settings.cas_export_url)
            response.raise_for_status()
            content = response.content

        if not content:
            raise ValueError("CAS 导出文件为空")

        return content

    def _lookup_local(self, user_id: int) -> CASCheckResult | None:
        """从内存快照中查询用户"""
        if self._snapshot is None:
            return None

        record = self._snapshot.get(user_id)
        if record is None:
            return CASCheckResult(is_banned=False, user_id=user_id, cached=True)

        offenses, time_added_epoch = record
        time_added = None
        if time_added_epoch is not None:
            with contextlib.suppress(Exception):
                time_added = datetime.fromtimestamp(time_added_epoch, tz=UTC)

        return CASCheckResult(
            is_banned=True,
            user_id=user_id,
            offenses=offenses,
            time_added=time_added,
            cached=True,
        )

    def _warn_if_snapshot_stale(self) -> None:
        """当快照过旧时输出告警日志"""
        if self._last_success_at is None:
            return

        age_seconds = int((datetime.now(UTC) - self._last_success_at).total_seconds())
        if age_seconds <= settings.cas_stale_after_seconds:
            return

        logger.warning(
            f"CAS 本地快照已过旧 [age:{age_seconds}s] "
            f"[last_success_at:{self._last_success_at.isoformat()}]"
        )

    @staticmethod
    def _parse_snapshot_file(path: Path) -> dict[int, tuple[int, int | None]]:
        """解析 CAS 导出 CSV，返回 user_id -> (offenses, time_added_epoch)"""
        snapshot: dict[int, tuple[int, int | None]] = {}

        with path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.reader(file)
            first_row = next(reader, None)
            if first_row is None:
                raise ValueError("CAS 快照为空")

            if CASService._parse_int(first_row[0] if first_row else None) is None:
                header = [CASService._normalize_header(value) for value in first_row]
                user_id_index = CASService._find_column_index(header, "user_id", "userid", "id")
                if user_id_index is None:
                    raise ValueError("CAS 快照缺少 user_id 列")
                offenses_index = CASService._find_column_index(
                    header, "offenses", "messages", "count"
                )
                time_added_index = CASService._find_column_index(
                    header,
                    "time_added",
                    "added_at",
                    "created_at",
                    "timestamp",
                )
            else:
                user_id_index = 0
                offenses_index = 1 if len(first_row) > 1 else None
                time_added_index = 2 if len(first_row) > 2 else None
                CASService._append_snapshot_row(
                    snapshot,
                    first_row,
                    user_id_index=user_id_index,
                    offenses_index=offenses_index,
                    time_added_index=time_added_index,
                )

            for row in reader:
                CASService._append_snapshot_row(
                    snapshot,
                    row,
                    user_id_index=user_id_index,
                    offenses_index=offenses_index,
                    time_added_index=time_added_index,
                )

        if not snapshot:
            raise ValueError("CAS 快照中没有有效用户")

        return snapshot

    @staticmethod
    def _append_snapshot_row(
        snapshot: dict[int, tuple[int, int | None]],
        row: list[str],
        *,
        user_id_index: int,
        offenses_index: int | None,
        time_added_index: int | None,
    ) -> None:
        if not row or user_id_index >= len(row):
            return

        user_id = CASService._parse_int(row[user_id_index])
        if user_id is None:
            return

        offenses = 0
        if offenses_index is not None and offenses_index < len(row):
            offenses = CASService._parse_int(row[offenses_index]) or 0

        time_added_epoch = None
        if time_added_index is not None and time_added_index < len(row):
            time_added_epoch = CASService._parse_time_added_epoch(row[time_added_index])

        snapshot[user_id] = (offenses, time_added_epoch)

    @staticmethod
    def _find_column_index(header: list[str], *aliases: str) -> int | None:
        for alias in aliases:
            with contextlib.suppress(ValueError):
                return header.index(alias)
        return None

    @staticmethod
    def _normalize_header(value: str) -> str:
        return value.lstrip("\ufeff").strip().lower()

    @staticmethod
    def _parse_int(value: str | None) -> int | None:
        if value is None:
            return None

        text = str(value).lstrip("\ufeff").strip()
        if not text:
            return None

        with contextlib.suppress(ValueError):
            return int(float(text))
        return None

    @staticmethod
    def _parse_time_added_epoch(value: str | None) -> int | None:
        if value is None:
            return None

        text = str(value).strip()
        if not text:
            return None

        parsed_int = CASService._parse_int(text)
        if parsed_int is not None:
            return parsed_int

        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return int(parsed.timestamp())

        return None


_cas_service: CASService | None = None


def get_cas_service() -> CASService:
    """获取 CAS 服务单例"""

    global _cas_service
    if _cas_service is None:
        _cas_service = CASService()
    return _cas_service
