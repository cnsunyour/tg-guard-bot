"""ModerationService error code 化契约 + handler 渲染 + catalog parity 测试(3c12)。

验证:
- ModerationResult.success 由 code 推导(code is None = 成功)
- verify_user_in_chat / verify_not_admin 返回稳定 code(left/kicked/creator/administrator + API 异常)
- ban/kick/mute/ban_temp API 异常统一 operation_failed code
- handler 据 code 选 moderation.error.<code>.message 渲染(cmd_kick / cmd_ban 失败分支)
- _process_report_approval 封禁失败走 moderation.report.approval.ban_failed.message(escape error)
- catalog 三语 parity(5 error + 1 report ban_failed)
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import moderation as moderation_handler
from src.services.moderation import (
    ModerationErrorCode,
    ModerationResult,
    ModerationService,
)

pytestmark = pytest.mark.unit

CHAT_ID = -100123
USER_ID = 42
OPERATOR_ID = 7


# ===== ModerationResult 计算属性 =====
def test_moderation_result_success_only_when_code_is_none() -> None:
    """success 由 code 推导:None=成功,任意 code=失败。"""
    assert ModerationResult().success is True
    assert ModerationResult().code is None
    for code in (
        ModerationErrorCode.user_not_in_chat,
        ModerationErrorCode.verify_user_failed,
        ModerationErrorCode.target_is_admin,
        ModerationErrorCode.verify_admin_failed,
        ModerationErrorCode.operation_failed,
    ):
        result = ModerationResult(code=code)
        assert result.success is False
        assert result.code is code


def test_moderation_error_code_values_are_stable_strings() -> None:
    """StrEnum value 是稳定字符串(catalog key 依赖)。"""
    assert ModerationErrorCode.user_not_in_chat.value == "user_not_in_chat"
    assert ModerationErrorCode.verify_user_failed.value == "verify_user_failed"
    assert ModerationErrorCode.target_is_admin.value == "target_is_admin"
    assert ModerationErrorCode.verify_admin_failed.value == "verify_admin_failed"
    assert ModerationErrorCode.operation_failed.value == "operation_failed"


# ===== verify_user_in_chat / verify_not_admin 稳定 code =====
@pytest.mark.parametrize(
    ("method_name", "status", "expected_code"),
    [
        ("verify_user_in_chat", "left", ModerationErrorCode.user_not_in_chat),
        ("verify_user_in_chat", "kicked", ModerationErrorCode.user_not_in_chat),
        ("verify_not_admin", "creator", ModerationErrorCode.target_is_admin),
        ("verify_not_admin", "administrator", ModerationErrorCode.target_is_admin),
    ],
)
async def test_member_status_checks_return_stable_codes(
    method_name: str,
    status: str,
    expected_code: ModerationErrorCode,
) -> None:
    """left/kicked → user_not_in_chat;creator/administrator → target_is_admin。"""
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=status))

    result = await getattr(ModerationService, method_name)(bot, CHAT_ID, USER_ID)

    assert result.success is False
    assert result.code is expected_code


@pytest.mark.parametrize(
    ("method_name", "expected_code"),
    [
        ("verify_user_in_chat", ModerationErrorCode.verify_user_failed),
        ("verify_not_admin", ModerationErrorCode.verify_admin_failed),
    ],
)
async def test_member_checks_convert_api_errors_to_codes(
    method_name: str,
    expected_code: ModerationErrorCode,
) -> None:
    """get_chat_member 抛异常 → verify_*_failed(不传播,仅 debug 日志)。"""
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(side_effect=RuntimeError("Telegram API error"))

    result = await getattr(ModerationService, method_name)(bot, CHAT_ID, USER_ID)

    assert result.success is False
    assert result.code is expected_code


@pytest.mark.parametrize(
    ("method_name", "kwargs", "failing_api"),
    [
        ("kick_user", {}, "ban_chat_member"),
        ("mute_user", {"duration": 10}, "restrict_chat_member"),
        ("ban_user", {"allow_left": True}, "ban_chat_member"),
        ("ban_user_temporarily", {"duration": 10}, "ban_chat_member"),
    ],
)
async def test_moderation_actions_share_operation_failed_code(
    mocker,
    method_name: str,
    kwargs: dict[str, object],
    failing_api: str,
) -> None:
    """ban/kick/mute/ban_temp API 异常统一返回 operation_failed(文案不参与业务)。"""
    bot = MagicMock()
    setattr(bot, failing_api, AsyncMock(side_effect=RuntimeError("forbidden")))
    bot.get_chat_member = AsyncMock()
    # verify_* 放行,聚焦处罚 API 抛异常分支
    mocker.patch.object(
        ModerationService, "verify_user_in_chat", new=AsyncMock(return_value=ModerationResult())
    )
    mocker.patch.object(
        ModerationService, "verify_not_admin", new=AsyncMock(return_value=ModerationResult())
    )
    mocker.patch.object(
        ModerationService, "_unban_or_unmute_user", new=AsyncMock(return_value=True)
    )
    mocker.patch("src.services.moderation.AuditRepository.log_action", new=AsyncMock())

    result = await getattr(ModerationService, method_name)(
        bot, CHAT_ID, USER_ID, OPERATOR_ID, **kwargs
    )

    assert result.success is False
    assert result.code is ModerationErrorCode.operation_failed


# ===== catalog 三语 parity =====
def test_moderation_catalog_has_three_locale_parity() -> None:
    """6 个新 key 三语均存在(parity 由 catalog 校验器保证,此处锁 key 集合)。"""
    from src.core.i18n.catalog import load_catalogs

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    expected_keys = {
        "moderation.error.operation_failed.message",
        "moderation.error.target_is_admin.message",
        "moderation.error.user_not_in_chat.message",
        "moderation.error.verify_admin_failed.message",
        "moderation.error.verify_user_failed.message",
        "moderation.report.approval.ban_failed.message",
    }
    for catalog in catalogs.values():
        assert expected_keys <= catalog.keys()


# ===== handler 据 code 渲染 =====
def _localizer() -> MagicMock:
    loc = MagicMock()

    def fake_t(key, **kw):
        return f"<{key}>" if not kw else f"<{key}:{kw}>"

    loc.t.side_effect = fake_t
    return loc


# ===== _render_warning_reason 渲染（/warnings reason 字段）=====
def test_render_warning_reason_system_code_uses_catalog() -> None:
    """系统警告 code → catalog 渲染；历史中文值也映射（兼容旧 warnings.reason 记录）。"""
    localizer = _localizer()
    expected = "<moderation.warnings.system_reason.channel_impersonation.label>"
    assert (
        moderation_handler._render_warning_reason(localizer, "system:channel_impersonation")
        == expected
    )
    # 历史兼容：修复前直接写入 warnings.reason 的中文也走同一 catalog key
    assert moderation_handler._render_warning_reason(localizer, "使用频道马甲发言") == expected


def test_render_warning_reason_free_text_escapes() -> None:
    """管理员自由输入文本 → escape_html；不调 catalog。"""
    localizer = _localizer()
    result = moderation_handler._render_warning_reason(localizer, "发了<广告> & 骚扰")
    assert result == "发了&lt;广告&gt; &amp; 骚扰"


def test_render_warning_reason_none_falls_back_to_no_reason() -> None:
    """空 reason → moderation.warnings.no_reason.label。"""
    localizer = _localizer()
    assert (
        moderation_handler._render_warning_reason(localizer, None)
        == "<moderation.warnings.no_reason.label>"
    )
    assert (
        moderation_handler._render_warning_reason(localizer, "")
        == "<moderation.warnings.no_reason.label>"
    )


def _message() -> MagicMock:
    message = MagicMock()
    message.chat.type = "group"
    message.chat.id = CHAT_ID
    message.from_user = SimpleNamespace(id=OPERATOR_ID)
    message.text = "/kick 42"
    message.reply_to_message = None
    message.answer = AsyncMock(return_value=MagicMock())
    return message


async def test_cmd_kick_renders_error_code_with_localizer(mocker) -> None:
    """kick 失败 → message.answer 用 moderation.error.<code>.message 渲染。"""
    localizer = _localizer()
    message = _message()
    mocker.patch.object(
        moderation_handler,
        "check_admin_permission_strict_message",
        new=AsyncMock(return_value=True),
    )
    mocker.patch.object(
        moderation_handler, "parse_user_from_message", new=AsyncMock(return_value=USER_ID)
    )
    mocker.patch.object(moderation_handler, "parse_moderation_args", return_value=(False, None))
    mocker.patch.object(
        moderation_handler.ModerationService,
        "kick_user",
        new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.user_not_in_chat)),
    )
    mocker.patch.object(moderation_handler, "auto_delete_message", new=AsyncMock())

    await moderation_handler.cmd_kick(message, MagicMock(), localizer)

    localizer.t.assert_called_once_with("moderation.error.user_not_in_chat.message")
    assert message.answer.await_args.args[0] == "❌ <moderation.error.user_not_in_chat.message>"


async def test_cmd_ban_renders_operation_failed(mocker) -> None:
    """ban 操作失败 → moderation.error.operation_failed.message。"""
    localizer = _localizer()
    message = _message()
    message.text = "/ban 42"
    mocker.patch.object(
        moderation_handler,
        "check_admin_permission_strict_message",
        new=AsyncMock(return_value=True),
    )
    mocker.patch.object(
        moderation_handler, "parse_user_from_message", new=AsyncMock(return_value=USER_ID)
    )
    mocker.patch.object(moderation_handler, "parse_moderation_args", return_value=(False, None))
    mocker.patch.object(
        moderation_handler.ModerationService,
        "ban_user",
        new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.operation_failed)),
    )
    mocker.patch.object(moderation_handler, "auto_delete_message", new=AsyncMock())

    await moderation_handler.cmd_ban(message, MagicMock(), localizer)

    localizer.t.assert_called_once_with("moderation.error.operation_failed.message")
    assert message.answer.await_args.args[0] == "❌ <moderation.error.operation_failed.message>"


# ===== _process_report_approval 封禁失败 =====
async def test_report_approval_ban_failure_localizes_and_escapes_error(mocker) -> None:
    """ban 失败 → moderation.report.approval.ban_failed.message,error 注入 escape 后的 code 文案。"""
    report = SimpleNamespace(
        group_id=CHAT_ID,
        status="pending",
        reported_user_id=USER_ID,
        reason="spam",
        message_id=100,
    )
    mocker.patch.object(
        moderation_handler.ReportRepository,
        "get_report_by_id",
        new=AsyncMock(return_value=report),
    )
    mocker.patch.object(
        moderation_handler.ModerationService,
        "ban_user",
        new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.operation_failed)),
    )

    localizer = MagicMock()

    def translate(key, **variables):
        if key == "moderation.error.operation_failed.message":
            return "操作失败，请检查 Bot 权限"
        if key == "moderation.report.approval.ban_failed.message":
            return f"ban failed: {variables['error']}"
        return key

    localizer.t.side_effect = translate

    success, msg = await moderation_handler._process_report_approval(
        bot=MagicMock(),
        report_id=1,
        chat_id=CHAT_ID,
        operator_id=OPERATOR_ID,
        localizer=localizer,
    )

    assert success is False
    # moderation.error 文案经 escape_html 后(无 HTML 字符则不变)注入 ban_failed
    assert msg == "ban failed: 操作失败，请检查 Bot 权限"
    localizer.t.assert_any_call("moderation.error.operation_failed.message")
    localizer.t.assert_any_call(
        "moderation.report.approval.ban_failed.message",
        error="操作失败，请检查 Bot 权限",
    )


async def test_report_approval_ban_failure_escapes_html_in_error(mocker) -> None:
    """moderation code 文案若含 HTML 字符(未来扩展),注入前 escape_html。"""
    report = SimpleNamespace(
        group_id=CHAT_ID,
        status="pending",
        reported_user_id=USER_ID,
        reason="spam",
        message_id=100,
    )
    mocker.patch.object(
        moderation_handler.ReportRepository,
        "get_report_by_id",
        new=AsyncMock(return_value=report),
    )
    mocker.patch.object(
        moderation_handler.ModerationService,
        "ban_user",
        new=AsyncMock(return_value=ModerationResult(code=ModerationErrorCode.verify_user_failed)),
    )

    localizer = MagicMock()

    def translate(key, **variables):
        if key == "moderation.error.verify_user_failed.message":
            return "<script>x</script>"  # 模拟未来含 HTML 的文案
        if key == "moderation.report.approval.ban_failed.message":
            return f"ban failed: {variables['error']}"
        return key

    localizer.t.side_effect = translate

    _success, msg = await moderation_handler._process_report_approval(
        bot=MagicMock(),
        report_id=1,
        chat_id=CHAT_ID,
        operator_id=OPERATOR_ID,
        localizer=localizer,
    )

    assert msg == "ban failed: &lt;script&gt;x&lt;/script&gt;"
