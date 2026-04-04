"""CAS 服务测试"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.services.cas_service import CASService


@pytest.fixture
def cas_service(tmp_path):
    """创建 CAS 服务实例"""
    service = CASService()
    service._snapshot_path = tmp_path / "export.csv"
    return service


@pytest.mark.asyncio
async def test_check_user_without_snapshot_fail_open(cas_service):
    """没有可用快照时应降级放行"""
    result = await cas_service.check_user(123456789)

    assert result.is_banned is False
    assert result.user_id == 123456789
    assert result.cached is False
    assert result.error == "snapshot_unavailable"


@pytest.mark.asyncio
async def test_check_user_banned_from_loaded_snapshot(cas_service):
    """测试从本地快照命中黑名单用户"""
    cas_service._snapshot = {123456789: (3, 1234567890)}

    result = await cas_service.check_user(123456789)

    assert result.is_banned is True
    assert result.user_id == 123456789
    assert result.offenses == 3
    assert result.cached is True
    assert result.error is None
    assert result.time_added == datetime.fromtimestamp(1234567890, tz=UTC)


@pytest.mark.asyncio
async def test_check_user_not_banned_from_loaded_snapshot(cas_service):
    """测试从本地快照查询正常用户"""
    cas_service._snapshot = {111: (1, 100)}

    result = await cas_service.check_user(222)

    assert result.is_banned is False
    assert result.user_id == 222
    assert result.cached is True
    assert result.error is None


@pytest.mark.asyncio
async def test_load_snapshot_success(cas_service):
    """测试加载仅包含 user_id 的 CAS 快照"""
    cas_service._snapshot_path.write_text("6151334747\n6134074488\n", encoding="utf-8")

    loaded = await cas_service._load_snapshot()

    assert loaded is True
    assert cas_service._record_count == 2
    assert cas_service._snapshot == {
        6151334747: (0, None),
        6134074488: (0, None),
    }
    assert cas_service._last_success_at is not None


@pytest.mark.asyncio
async def test_parse_snapshot_file_with_header_and_extra_fields(tmp_path):
    """测试解析带表头的快照文件"""
    path = tmp_path / "header.csv"
    path.write_text(
        "user_id,offenses,time_added,extra\n123,5,1234567890,x\n456,1,2026-04-05T12:30:00+00:00,y\n",
        encoding="utf-8",
    )

    snapshot = CASService._parse_snapshot_file(path)

    assert snapshot == {
        123: (5, 1234567890),
        456: (1, int(datetime(2026, 4, 5, 12, 30, tzinfo=UTC).timestamp())),
    }


@pytest.mark.asyncio
async def test_parse_snapshot_file_skips_invalid_rows(tmp_path):
    """测试坏行不会影响其他正常行"""
    path = tmp_path / "invalid.csv"
    path.write_text("123\nabc\n\n456\n", encoding="utf-8")

    snapshot = CASService._parse_snapshot_file(path)

    assert snapshot == {
        123: (0, None),
        456: (0, None),
    }


@pytest.mark.asyncio
async def test_refresh_once_keeps_old_snapshot_on_parse_failure(cas_service, monkeypatch):
    """刷新失败时保留旧快照"""
    cas_service._snapshot = {1: (2, None)}
    cas_service._record_count = 1
    old_time = datetime.now(UTC) - timedelta(hours=1)
    cas_service._last_success_at = old_time

    async def fake_download():
        return b""

    monkeypatch.setattr(cas_service, "_download_snapshot_bytes", fake_download)

    refreshed = await cas_service._refresh_once(reason="test")

    assert refreshed is False
    assert cas_service._snapshot == {1: (2, None)}
    assert cas_service._record_count == 1
    assert cas_service._last_success_at == old_time


@pytest.mark.asyncio
async def test_refresh_once_replaces_snapshot_on_success(cas_service, monkeypatch):
    """刷新成功后应替换快照"""
    cas_service._snapshot = {1: (2, None)}

    async def fake_download():
        return b"123\n456\n"

    monkeypatch.setattr(cas_service, "_download_snapshot_bytes", fake_download)

    refreshed = await cas_service._refresh_once(reason="test")

    assert refreshed is True
    assert cas_service._snapshot == {
        123: (0, None),
        456: (0, None),
    }
    assert cas_service._record_count == 2
    assert cas_service._last_success_at is not None
    assert cas_service._snapshot_path.read_text(encoding="utf-8") == "123\n456\n"


@pytest.mark.asyncio
async def test_close_cancels_refresh_task(cas_service):
    """关闭服务时应取消后台任务"""

    async def never_end():
        await asyncio.sleep(3600)

    cas_service._refresh_task = asyncio.create_task(never_end())
    await cas_service.close()

    assert cas_service._refresh_task is None


@pytest.mark.asyncio
async def test_parse_snapshot_file_supports_bom(tmp_path):
    """测试带 BOM 的 headerless 快照也能解析"""
    path = tmp_path / "bom.csv"
    path.write_text("\ufeff123\n456\n", encoding="utf-8")

    snapshot = CASService._parse_snapshot_file(path)

    assert snapshot == {
        123: (0, None),
        456: (0, None),
    }
