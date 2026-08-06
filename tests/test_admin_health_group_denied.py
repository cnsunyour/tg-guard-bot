"""/health 群内可见性安全加固测试（C 段 #9，原 #10）。

健康报告含 DB/Redis/system 敏感信息，群内对所有成员可见 → 仅私聊执行。
覆盖：
- 群聊（group/supergroup）超管执行 → group_denied.message + 不调 health_checker
- 私聊超管执行 → 不触发 group_denied，走完 health_checker 成功路径
- catalog group_denied.message 三语存在
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.handlers.admin import cmd_health

pytestmark = pytest.mark.unit

ADMIN_ID = 12345


def _message(chat_type: str, user_id: int = ADMIN_ID) -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = -100
    message.from_user.id = user_id
    message.answer = AsyncMock()
    return message


def _localizer() -> MagicMock:
    """mock localizer：t 返回 key 本身，便于断言（渲染成功不依赖具体文案）。"""
    loc = MagicMock()
    loc.t.side_effect = lambda key, **kw: key
    return loc


def _health_report() -> dict:
    """完整 health report schema（与 test_admin_health_stats._health_report 一致，
    避免 cmd_health 访问字段时 KeyError 误走 failed 路径）。"""
    return {
        "healthy": True,
        "check_count": 5,
        "uptime": {
            "days": 0,
            "hours": 0,
            "minutes": 0,
            "seconds": 0,
            "seconds_component": 0,
            "formatted": "0秒",
            "started_at": "2026-08-05T00:00:00",
        },
        "database": {"healthy": True, "latency_ms": 1.0, "error": None},
        "redis": {"healthy": True, "latency_ms": 0.5, "error": None},
        "system": {
            "cpu": {"percent": 10.0, "count": 4},
            "memory": {"used_mb": 100, "total_mb": 1000, "percent": 10.0},
            "disk": {"used_gb": 10, "total_gb": 100, "percent": 10.0},
        },
    }


async def test_health_group_chat_denied_even_for_super_admin() -> None:
    """群聊中超管执行 /health → group_denied.message，不调用 health_checker。"""
    message = _message("supergroup", ADMIN_ID)
    with (
        patch("src.bot.handlers.admin.settings.admin_ids", [ADMIN_ID]),
        patch("src.bot.handlers.admin.get_health_checker") as mock_get,
    ):
        await cmd_health(message, _localizer())
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "admin.health.group_denied.message"
    # 群聊拒绝在 health_checker 之前 return，不应调用
    mock_get.assert_not_called()


async def test_health_private_chat_proceeds_without_group_denied() -> None:
    """私聊超管执行 /health → 不触发 group_denied，走完 health_checker 成功路径。"""
    message = _message("private", ADMIN_ID)
    with (
        patch("src.bot.handlers.admin.settings.admin_ids", [ADMIN_ID]),
        patch("src.bot.handlers.admin.get_health_checker") as mock_get,
    ):
        mock_checker = MagicMock()
        mock_checker.full_check = AsyncMock(return_value=_health_report())
        mock_get.return_value = mock_checker
        await cmd_health(message, _localizer())
    # 私聊进入 health_checker 流程（群聊检查未拦截）
    mock_get.assert_called_once()
    answered = [c.args[0] for c in message.answer.await_args_list]
    assert "admin.health.group_denied.message" not in answered
    assert "admin.health.permission_denied.message" not in answered


def test_group_denied_key_exists_in_three_locales() -> None:
    """admin.health.group_denied.message 三语存在。"""
    root = Path(__file__).resolve().parents[1]
    for locale in ("zh-Hans", "zh-Hant", "en"):
        catalog = json.loads((root / "locales" / f"{locale}.json").read_text("utf-8"))
        assert "admin.health.group_denied.message" in catalog, f"{locale} 缺 key"
