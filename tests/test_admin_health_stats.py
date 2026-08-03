"""cmd_health + cmd_stats i18n 测试（3c3）。

验证 /health /stats 报告走 catalog（report.message + uptime 本地化 +
条件段 db_error_line/system_block 预渲染），超级管理员权限拒绝。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

SUPER_ADMIN = 1
NON_ADMIN = 999


def _message(user_id: int = SUPER_ADMIN) -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=user_id)
    message.answer = AsyncMock()
    return message


def _uptime() -> dict:
    return {
        "days": 1,
        "hours": 2,
        "minutes": 3,
        "seconds_component": 4,
        "seconds": 93784,
        "formatted": "1天 2小时 3分钟 4秒",
        "started_at": "2026-08-03T00:00:00",
    }


def _health_report(db_error: str | None = None, with_system: bool = True) -> dict:
    report: dict = {
        "healthy": True,
        "check_count": 5,
        "uptime": _uptime(),
        "database": {"healthy": True, "latency_ms": 1.23, "error": db_error},
        "redis": {"healthy": True, "latency_ms": 0.5, "error": None},
    }
    if with_system:
        report["system"] = {
            "cpu": {"percent": 10.0, "count": 4},
            "memory": {"used_mb": 100, "total_mb": 1000, "percent": 10.0},
            "disk": {"used_gb": 10, "total_gb": 100, "percent": 10.0},
        }
    return report


def _localizer() -> MagicMock:
    """localizer.t side_effect：带 kw 返回 <key:kw>，无 kw 返回 <key>。"""
    localizer = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}:{kw}>" if kw else f"<{key}>"

    localizer.t.side_effect = fake_t
    return localizer


# ===== health =====
async def test_health_non_super_admin_denied(mocker) -> None:
    """非超级管理员 → permission_denied key。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    localizer = _localizer()
    message = _message(user_id=NON_ADMIN)

    await handler.cmd_health(message, localizer)

    localizer.t.assert_called_once_with("admin.health.permission_denied.message")


async def test_health_report_all_placeholders(mocker) -> None:
    """成功（system + 无 error）→ report.message 10 占位符齐全。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    health_checker = AsyncMock()
    health_checker.full_check = AsyncMock(return_value=_health_report())
    mocker.patch.object(handler, "get_health_checker", return_value=health_checker)
    localizer = _localizer()
    message = _message()

    await handler.cmd_health(message, localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.health.report.message",)
    # db_error_line/redis_error_line 为空串（无 error）
    assert last.kwargs["db_error_line"] == ""
    assert last.kwargs["redis_error_line"] == ""
    # system 段预渲染（含 system.message key）+ uptime 本地化模板
    assert "admin.health.report.system.message" in last.kwargs["system_block"]
    assert "admin.common.uptime.message" in last.kwargs["uptime"]


async def test_health_db_error_renders_error_line(mocker) -> None:
    """db error → db_error_line 预渲染 error_line.message。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    health_checker = AsyncMock()
    health_checker.full_check = AsyncMock(
        return_value=_health_report(db_error="connection refused", with_system=False)
    )
    mocker.patch.object(handler, "get_health_checker", return_value=health_checker)
    localizer = _localizer()
    message = _message()

    await handler.cmd_health(message, localizer)

    last = localizer.t.call_args
    assert "admin.health.error_line.message" in last.kwargs["db_error_line"]
    assert last.kwargs["redis_error_line"] == ""  # redis 无 error
    assert last.kwargs["system_block"] == ""  # with_system=False


async def test_health_exception_returns_failed(mocker) -> None:
    """full_check 抛异常 → failed key。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    health_checker = AsyncMock()
    health_checker.full_check = AsyncMock(side_effect=RuntimeError("db down"))
    mocker.patch.object(handler, "get_health_checker", return_value=health_checker)
    localizer = _localizer()
    message = _message()

    await handler.cmd_health(message, localizer)

    localizer.t.assert_called_with("admin.health.failed.message")


# ===== stats =====
async def test_stats_non_super_admin_denied(mocker) -> None:
    """非超级管理员 → permission_denied key。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    localizer = _localizer()
    message = _message(user_id=NON_ADMIN)

    await handler.cmd_stats(message, localizer)

    localizer.t.assert_called_once_with("admin.stats.permission_denied.message")


async def test_stats_report_with_classifier_status(mocker) -> None:
    """成功 → report.message 含 classifier/embedder label 预渲染。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    detector = AsyncMock()
    detector.get_statistics = AsyncMock(
        return_value={
            "total_samples": 100,
            "spam_samples": 30,
            "normal_samples": 70,
            "classifier_trained": True,
            "embedder_initialized": False,
        }
    )
    mocker.patch("src.services.spam_detector.get_detector", return_value=detector)
    health_checker = AsyncMock()
    health_checker.get_uptime = MagicMock(return_value=_uptime())
    mocker.patch.object(handler, "get_health_checker", return_value=health_checker)
    localizer = _localizer()
    message = _message()

    await handler.cmd_stats(message, localizer)

    last = localizer.t.call_args
    assert last.args == ("admin.stats.report.message",)
    assert last.kwargs["classifier_status"] == "<admin.stats.classifier.trained.label>"
    assert last.kwargs["embedder_status"] == "<admin.stats.embedder.uninitialized.label>"
    assert last.kwargs["total"] == 100


async def test_health_db_error_html_escaped(mocker) -> None:
    """db error 含 HTML 字符 → escape_html 后插入 error_line，防 HTML 破坏报告。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    health_checker = AsyncMock()
    health_checker.full_check = AsyncMock(
        return_value=_health_report(db_error="<node> & co", with_system=False)
    )
    mocker.patch.object(handler, "get_health_checker", return_value=health_checker)
    localizer = _localizer()
    message = _message()

    await handler.cmd_health(message, localizer)

    last = localizer.t.call_args
    # error 经 escape_html：<node> → &lt;node&gt;, & → &amp;
    assert "&lt;node&gt;" in last.kwargs["db_error_line"]
    assert "&amp;" in last.kwargs["db_error_line"]
    assert "<node>" not in last.kwargs["db_error_line"]


async def test_stats_exception_returns_failed(mocker) -> None:
    """get_statistics 抛异常 → failed key。"""
    mocker.patch.object(handler.settings, "admin_ids", {SUPER_ADMIN})
    detector = AsyncMock()
    detector.get_statistics = AsyncMock(side_effect=RuntimeError("detector down"))
    mocker.patch("src.services.spam_detector.get_detector", return_value=detector)
    mocker.patch.object(handler, "get_health_checker", return_value=AsyncMock())
    localizer = _localizer()
    message = _message()

    await handler.cmd_stats(message, localizer)

    localizer.t.assert_called_with("admin.stats.failed.message")
