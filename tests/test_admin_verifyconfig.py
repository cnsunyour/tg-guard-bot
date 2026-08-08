"""cmd_verify_config i18n + 权限校验测试。

验证 /verifyconfig 报告走 catalog（report.message + 公共词项
verification_type/status），未知验证类型回退到 code。
含管理员权限校验（L1）：非管理员返回 admin_only key，私聊不查权限。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import admin as handler

pytestmark = pytest.mark.unit

CHAT_ID = -100


def _message(chat_type: str = "group") -> MagicMock:
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = CHAT_ID
    message.chat.title = "Test"
    message.from_user = MagicMock(id=42)
    message.answer = AsyncMock(return_value=MagicMock())
    message.delete = AsyncMock()
    return message


def _group(**overrides) -> MagicMock:
    defaults = {
        "verification_type": "math",
        "verification_timeout": 120,
        "antispam_enabled": True,
        "antispam_level": 2,
        "activity_enabled": False,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


def _patch(mocker, group: MagicMock, *, is_admin: bool = True) -> MagicMock:
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=is_admin))
    # localizer.t 真实模拟：返回 key 本身（缺词项回退场景）或带占位符标记
    localizer = MagicMock()

    def fake_t(key, **kw):
        if kw:
            return f"<{key}:{kw}>"
        return f"<{key}>"

    localizer.t.side_effect = fake_t
    return localizer


async def test_private_chat_rejected(mocker) -> None:
    """私聊 → group_only key，且不查管理员权限（先私聊再权限）。"""
    localizer = _patch(mocker, _group())
    message = _message(chat_type="private")

    await handler.cmd_verify_config(message, MagicMock(), localizer)

    localizer.t.assert_called_once_with("admin.verifyconfig.error.group_only.message")
    handler.check_admin_permission.assert_not_called()


async def test_non_admin_rejected(mocker) -> None:
    """非管理员 → admin_only key，不查询群组配置。"""
    localizer = _patch(mocker, _group(), is_admin=False)
    message = _message()

    await handler.cmd_verify_config(message, MagicMock(), localizer)

    localizer.t.assert_called_once_with("admin.verifyconfig.error.admin_only.message")
    handler.GroupRepository.get_or_create.assert_not_called()


async def test_report_passes_all_placeholders(mocker) -> None:
    """成功 → report.message 含 5 个占位符（verify_type/timeout/antispam_status/antispam_level/activity_status）。"""
    group = _group(
        verification_type="slider",
        verification_timeout=90,
        antispam_enabled=False,
        antispam_level=1,
        activity_enabled=True,
    )
    localizer = _patch(mocker, group)
    message = _message()

    await handler.cmd_verify_config(message, MagicMock(), localizer)

    # 最后一次 t 调用应为 report.message 且 5 占位符齐全
    last_call = localizer.t.call_args
    assert last_call.args == ("admin.verifyconfig.report.message",)
    assert last_call.kwargs == {
        "verify_type": "<admin.common.verification_type.slider.label>",
        "timeout": 90,
        "antispam_status": "<admin.common.status.disabled.label>",
        "antispam_level": 1,
        "activity_status": "<admin.common.status.enabled.label>",
    }


async def test_unknown_verify_type_falls_back_to_code(mocker) -> None:
    """未知 verification_type（catalog 无对应 label）→ 回退原始 code，不翻译。

    模拟真实 Translator：缺失 key 返回 key 本身 → 代码检测后回退到 code。
    """
    group = _group(verification_type="future_type")
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    localizer = MagicMock()
    missing = "admin.common.verification_type.future_type.label"

    def fake_t(key, **kw):
        if kw:
            return f"<{key}:{kw}>"
        if key == missing:
            return key  # 模拟 Translator 缺失 key → 返回 key 本身
        return f"<{key}>"

    localizer.t.side_effect = fake_t
    message = _message()

    await handler.cmd_verify_config(message, MagicMock(), localizer)

    last_call = localizer.t.call_args
    assert last_call.kwargs["verify_type"] == "future_type"


async def test_load_failure_returns_load_failed_key(mocker) -> None:
    """get_or_create 抛异常 → load_failed key。"""
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    mocker.patch.object(
        handler.GroupRepository,
        "get_or_create",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}>"
    message = _message()

    await handler.cmd_verify_config(message, MagicMock(), localizer)

    localizer.t.assert_called_once_with("admin.verifyconfig.error.load_failed.message")


async def test_strict_translator_keyerror_falls_back(mocker) -> None:
    """strict Translator 缺失 key 抛 KeyError → 回退 escape_html(code)，不触发外层 load_failed。"""
    group = _group(verification_type="future_type")
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    localizer = MagicMock()

    def fake_t(key, **kw):
        if kw:
            return f"<{key}:{kw}>"
        if key == "admin.common.verification_type.future_type.label":
            raise KeyError(key)  # 模拟 strict Translator 缺失
        return f"<{key}>"

    localizer.t.side_effect = fake_t
    message = _message()

    await handler.cmd_verify_config(message, MagicMock(), localizer)

    # KeyError 被捕获 → 回退 code（escape_html("future_type") 无特殊字符，原样）
    assert localizer.t.call_args.kwargs["verify_type"] == "future_type"
    # 未误报 load_failed（最后一次 t 是 report.message 而非 load_failed）
    assert localizer.t.call_args.args == ("admin.verifyconfig.report.message",)


async def test_unknown_code_html_chars_escaped(mocker) -> None:
    """DB 污染的 code 含 HTML 字符 → 回退值经 escape_html，防格式破坏/注入。"""
    group = _group(verification_type="<x>")  # 假设 DB 被直接写入非法值
    mocker.patch.object(handler, "auto_delete_message", new=AsyncMock())
    mocker.patch.object(handler.GroupRepository, "get_or_create", new=AsyncMock(return_value=group))
    mocker.patch.object(handler, "check_admin_permission", new=AsyncMock(return_value=True))
    localizer = MagicMock()
    missing = "admin.common.verification_type.<x>.label"

    def fake_t(key, **kw):
        if kw:
            return f"<{key}:{kw}>"
        if key == missing:
            return key
        return f"<{key}>"

    localizer.t.side_effect = fake_t
    message = _message()

    await handler.cmd_verify_config(message, MagicMock(), localizer)

    assert localizer.t.call_args.kwargs["verify_type"] == "&lt;x&gt;"


def test_en_qa_label_uses_html_entity() -> None:
    """en qa label 的 & 必须是 HTML 实体（Q&amp;A），否则 HTML parse_mode 解析错误。"""
    import json

    with open("locales/en.json", encoding="utf-8") as f:
        en = json.load(f)
    assert en["admin.common.verification_type.qa.label"] == "Q&amp;A"
