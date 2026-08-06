"""CAS 黑名单检查中间件测试"""

from pathlib import Path

import pytest


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
