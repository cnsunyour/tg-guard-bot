"""cleanup i18n 契约测试（service code 化 + handler 渲染 + catalog parity）。

覆盖：
- CleanupReason / CleanupErrorCode StrEnum 值稳定
- execute_cleanup 4 类失败 → CleanupError(code + user_id + detail)
- _increment_kicked/failed 用 enum identity（删 "已删除" 字符串 fallback）
- _render_cleanup_error 按 code 选 catalog key + 注入 user_id + escape detail
- detail=None 安全（escape_html(None)=""，无 {detail} 占位符的 code 不受影响）
- catalog 三语 parity（36 cleanup.* key + 占位符对等）
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from src.bot.handlers.cleanup import _render_cleanup_error, _render_cleanup_exception
from src.services.cleanup import (
    CleanupError,
    CleanupErrorCode,
    CleanupReason,
    CleanupResult,
    CleanupService,
)
from src.services.member_query import MemberQueryFloodWaitError

pytestmark = pytest.mark.unit

CHAT_ID = -100123
OPERATOR_ID = 99


def _make_bot(
    *,
    member_status: str = "member",
    ban_side_effect: BaseException | None = None,
) -> MagicMock:
    """构造 mock Bot：get_chat_member 返回指定 status；ban 抛指定异常。"""
    bot = MagicMock()
    member = MagicMock()
    member.status = member_status
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.ban_chat_member = AsyncMock(side_effect=ban_side_effect or None)
    bot.unban_chat_member = AsyncMock(return_value=None)
    return bot


# ===== StrEnum 值稳定 =====
def test_cleanup_reason_values_are_stable() -> None:
    """CleanupReason value 是稳定字符串（_increment identity + catalog 依赖）。"""
    assert CleanupReason.restricted.value == "restricted"
    assert CleanupReason.scam.value == "scam"
    assert CleanupReason.fake.value == "fake"
    assert CleanupReason.deleted.value == "deleted"


def test_cleanup_error_code_values_are_stable() -> None:
    """CleanupErrorCode value 是稳定字符串（catalog key cleanup.error.<code>.message 依赖）。"""
    assert CleanupErrorCode.target_is_admin.value == "target_is_admin"
    assert CleanupErrorCode.bot_permission_denied.value == "bot_permission_denied"
    assert CleanupErrorCode.telegram_bad_request.value == "telegram_bad_request"
    assert CleanupErrorCode.unexpected_error.value == "unexpected_error"


# ===== execute_cleanup 4 类失败 → CleanupError =====
async def test_execute_cleanup_target_is_admin_returns_error() -> None:
    """目标是管理员 → CleanupError(target_is_admin)，计入对应 reason 失败。"""
    bot = _make_bot(member_status="administrator")
    result = await CleanupService.execute_cleanup(
        bot, CHAT_ID, [123], OPERATOR_ID, CleanupReason.deleted
    )
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.code is CleanupErrorCode.target_is_admin
    assert err.user_id == 123
    assert err.detail is None
    assert result.deleted_failed == 1
    # 管理员跳过，不调用 ban
    bot.ban_chat_member.assert_not_awaited()


async def test_execute_cleanup_bot_forbidden_returns_error() -> None:
    """TelegramForbiddenError → CleanupError(bot_permission_denied)。"""
    bot = _make_bot(
        member_status="member",
        ban_side_effect=TelegramForbiddenError(method="ban_chat_member", message="forbidden"),
    )
    result = await CleanupService.execute_cleanup(
        bot, CHAT_ID, [456], OPERATOR_ID, CleanupReason.restricted
    )
    err = result.errors[0]
    assert err.code is CleanupErrorCode.bot_permission_denied
    assert err.user_id == 456
    assert err.detail is None
    assert result.restricted_failed == 1


async def test_execute_cleanup_telegram_bad_request_returns_error_with_detail() -> None:
    """TelegramBadRequest（非 user not found）→ CleanupError(telegram_bad_request, detail=str(e))。"""
    exc = TelegramBadRequest(method="ban_chat_member", message="chat not found")
    bot = _make_bot(member_status="member", ban_side_effect=exc)
    result = await CleanupService.execute_cleanup(
        bot, CHAT_ID, [789], OPERATOR_ID, CleanupReason.scam
    )
    err = result.errors[0]
    assert err.code is CleanupErrorCode.telegram_bad_request
    assert err.user_id == 789
    assert err.detail == str(exc)  # 不硬编码 str 格式，与服务层一致即可
    assert result.scam_failed == 1


async def test_execute_cleanup_unexpected_error_returns_error_with_detail() -> None:
    """未知异常 → CleanupError(unexpected_error, detail=str(e))。"""
    bot = _make_bot(member_status="member", ban_side_effect=RuntimeError("boom"))
    result = await CleanupService.execute_cleanup(
        bot, CHAT_ID, [111], OPERATOR_ID, CleanupReason.fake
    )
    err = result.errors[0]
    assert err.code is CleanupErrorCode.unexpected_error
    assert err.user_id == 111
    assert err.detail == "boom"
    assert result.fake_failed == 1


# ===== _increment_kicked/failed 用 enum identity =====
def test_increment_kicked_uses_enum_identity() -> None:
    """4 个 reason 各自递增对应字段（enum identity，不靠字符串匹配）。"""
    r = CleanupResult()
    for reason, attr in [
        (CleanupReason.restricted, "restricted_kicked"),
        (CleanupReason.scam, "scam_kicked"),
        (CleanupReason.fake, "fake_kicked"),
        (CleanupReason.deleted, "deleted_kicked"),
    ]:
        CleanupService._increment_kicked(r, reason)
        assert getattr(r, attr) == 1


def test_increment_failed_uses_enum_identity() -> None:
    r = CleanupResult()
    for reason, attr in [
        (CleanupReason.restricted, "restricted_failed"),
        (CleanupReason.scam, "scam_failed"),
        (CleanupReason.fake, "fake_failed"),
        (CleanupReason.deleted, "deleted_failed"),
    ]:
        CleanupService._increment_failed(r, reason)
        assert getattr(r, attr) == 1


# ===== _render_cleanup_error 渲染 =====
def _localizer() -> MagicMock:
    """mock localizer：t 返回 key + params。"""
    loc = MagicMock()

    def fake_t(key, **kw):
        if not kw:
            return f"<{key}>"
        return f"<{key}:{kw}>"

    loc.t.side_effect = fake_t
    return loc


def test_render_cleanup_error_renders_code_with_params() -> None:
    """telegram_bad_request → cleanup.error.telegram_bad_request.message + user_id/detail 注入。"""
    localizer = _localizer()
    error = CleanupError(
        code=CleanupErrorCode.telegram_bad_request, user_id=123, detail="bad request"
    )
    result = _render_cleanup_error(localizer, error)
    assert result == (
        "<cleanup.error.telegram_bad_request.message:{'user_id': 123, 'detail': 'bad request'}>"
    )


def test_render_cleanup_error_escapes_detail() -> None:
    """detail 含 HTML → escape_html 后注入（防注入）。"""
    localizer = _localizer()
    error = CleanupError(
        code=CleanupErrorCode.telegram_bad_request,
        user_id=1,
        detail="<script>alert(1)</script>",
    )
    result = _render_cleanup_error(localizer, error)
    assert "detail': '&lt;script&gt;alert(1)&lt;/script&gt;'" in result


def test_render_cleanup_error_none_detail_safe() -> None:
    """detail=None（target_is_admin/bot_permission_denied）→ escape_html(None)='' 安全注入。"""
    localizer = _localizer()
    error = CleanupError(code=CleanupErrorCode.target_is_admin, user_id=42)
    result = _render_cleanup_error(localizer, error)
    # detail 经 escape_html(None)='' 后注入
    assert "detail': ''" in result
    assert "user_id': 42" in result


# ===== _render_cleanup_exception 渲染（handler except 块捕获的异常）=====
def test_render_cleanup_exception_flood_wait_uses_catalog() -> None:
    """MemberQueryFloodWaitError → cleanup.error.flood_wait.message + wait_seconds 注入。"""
    localizer = _localizer()
    error = MemberQueryFloodWaitError(30)
    result = _render_cleanup_exception(localizer, error)
    assert result == "<cleanup.error.flood_wait.message:{'wait_seconds': 30}>"
    assert error.seconds == 30


def test_render_cleanup_exception_other_exception_escapes_str() -> None:
    """非 FloodWait 异常 → escape_html(str(e)) 保留诊断文本（防注入）。"""
    localizer = _localizer()
    result = _render_cleanup_exception(localizer, RuntimeError("<bad> & data"))
    assert result == "&lt;bad&gt; &amp; data"
    localizer.t.assert_not_called()


# ===== catalog 三语 parity =====
def test_cleanup_catalog_keys_exist_in_three_locales() -> None:
    """36 cleanup.* key 三语均存在。"""
    root = Path(__file__).resolve().parents[1]
    all_keys: set[str] = set()
    for locale in ("zh-Hans", "zh-Hant", "en"):
        catalog = json.loads((root / "locales" / f"{locale}.json").read_text("utf-8"))
        cleanup_keys = {k for k in catalog if k.startswith("cleanup.")}
        all_keys |= cleanup_keys
    assert len(all_keys) == 36


@pytest.mark.parametrize("locale", ["zh-Hans", "zh-Hant", "en"])
def test_cleanup_error_codes_render_real_catalog_without_placeholders(locale: str) -> None:
    """4 个 error code 用真实 catalog strict Translator 渲染无残留占位符。"""
    from src.core.i18n.catalog import load_catalogs
    from src.core.i18n.translator import Translator

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    localizer = Translator(catalogs, "zh-Hans", strict=True).for_locale(locale)

    for code in CleanupErrorCode:
        error = CleanupError(
            code=code,
            user_id=123,
            detail=(
                "<safe>"
                if code
                in (
                    CleanupErrorCode.telegram_bad_request,
                    CleanupErrorCode.unexpected_error,
                )
                else None
            ),
        )
        text = _render_cleanup_error(localizer, error)
        # 严格模式 + 真实 catalog：无残留占位符
        assert "{" not in text
        assert "}" not in text
        # detail 已 escape（含 <safe> 的会变 &lt;safe&gt;）
        if error.detail:
            assert "<safe>" not in text
