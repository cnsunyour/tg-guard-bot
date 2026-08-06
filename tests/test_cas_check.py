"""CAS 黑名单检查中间件测试"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.types import Message

from src.bot.middlewares import cas_check


class _MockUser:
    """模拟 aiogram User 对象（仅需 id / first_name / full_name / username）"""

    def __init__(self, user_id, first_name, full_name=None, username=None):
        self.id = user_id
        self.first_name = first_name
        self.full_name = full_name
        self.username = username


def _real_localizer(locale: str = "zh-Hans"):
    """加载真实 catalog 的 localizer（验证文案渲染 + masked_mention_html 脱敏）。"""
    from src.core.i18n.catalog import load_catalogs
    from src.core.i18n.translator import Translator

    root = Path(__file__).resolve().parents[1]
    catalogs = load_catalogs(root / "locales", ["zh-Hans", "zh-Hant", "en"], "zh-Hans")
    return Translator(catalogs, default_locale="zh-Hans").for_locale(locale)


@pytest.mark.unit
def test_cas_notification_masks_user_name():
    """CAS 中间件通知应显示脱敏用户名（而非原始名或纯数字 ID）"""
    from src.bot.middlewares.cas_check import CASCheckMiddleware

    user = _MockUser(123456, "John", "John Doe广告", "johndoe")
    middleware = CASCheckMiddleware()
    localizer = _real_localizer()

    # CAS 黑名单通知（复用 verification.join.cas_ban.notify）
    text = middleware._get_notification_text(localizer, user, "cas_blacklist", {"offenses": 3})
    assert "John Doe广告" not in text  # 原始显示名不得泄露
    assert "tg://user?id=123456" in text  # 保留可点击链接（基于可信 ID）
    assert "123456</a>" not in text  # 链接文本不再是纯数字 ID
    assert "CAS 黑名单" in text
    assert "违规 3 次" not in text  # 产品决策：群内通知不展示违规次数（73b63fa）

    # 用户状态通知（诈骗账号，复用 verification.join.status_ban.*）
    text_scam = middleware._get_notification_text(
        localizer, user, "user_status_scam", {"status": "scam"}
    )
    assert "John Doe广告" not in text_scam
    assert "tg://user?id=123456" in text_scam
    assert "诈骗账号" in text_scam


@pytest.mark.unit
def test_cas_notification_unknown_status_falls_back_to_unknown_label():
    """未知 status → verification.join.status_ban.unknown.label（不展示裸 reason）。"""
    from src.bot.middlewares.cas_check import CASCheckMiddleware

    user = _MockUser(123456, "John", "John Doe广告", "johndoe")
    middleware = CASCheckMiddleware()
    localizer = _real_localizer()

    text = middleware._get_notification_text(
        localizer, user, "user_status_weird", {"status": "weird"}
    )
    assert "tg://user?id=123456" in text
    # 未知状态用 unknown.label（zh-Hans「未知状态」），不展示裸 weird
    assert "weird" not in text
    assert "未知状态" in text


@pytest.mark.unit
async def test_anonymous_message_bypasses_cas_checks(mocker, monkeypatch) -> None:
    """匿名管理员消息（sender_chat==chat）跳过 CAS / 用户状态检查"""
    monkeypatch.setattr(cas_check.settings, "cas_enabled", True)
    monkeypatch.setattr(cas_check.settings, "user_status_check_enabled", False)

    event = MagicMock(spec=Message)
    event.chat = SimpleNamespace(type="supergroup", id=-100123)
    event.from_user = SimpleNamespace(id=1087968824)
    event.sender_chat = SimpleNamespace(id=-100123)
    event.new_chat_members = None
    event.left_chat_member = None
    event.message_id = 1

    bot = MagicMock(spec=Bot)
    bot.id = 999
    localizer = MagicMock()
    handler = AsyncMock(return_value="handled")
    permission = mocker.patch.object(
        cas_check, "check_admin_permission", new=AsyncMock(return_value=True)
    )
    get_cas = mocker.patch.object(cas_check, "get_cas_service")

    result = await cas_check.CASCheckMiddleware()(
        handler, event, {"bot": bot, "localizer": localizer}
    )

    assert result == "handled"
    permission.assert_awaited_once_with(event, bot)
    get_cas.assert_not_called()
