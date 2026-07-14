"""CAS 黑名单检查中间件测试"""

import pytest


class _MockUser:
    """模拟 aiogram User 对象（仅需 id / first_name / full_name / username）"""

    def __init__(self, user_id, first_name, full_name=None, username=None):
        self.id = user_id
        self.first_name = first_name
        self.full_name = full_name
        self.username = username


@pytest.mark.unit
def test_cas_notification_masks_user_name():
    """CAS 中间件通知应显示脱敏用户名（而非原始名或纯数字 ID）"""
    from src.bot.middlewares.cas_check import CASCheckMiddleware

    user = _MockUser(123456, "John", "John Doe广告", "johndoe")
    middleware = CASCheckMiddleware()

    # CAS 黑名单通知
    text = middleware._get_notification_text(user, "cas_blacklist", {"offenses": 3})
    assert "John Doe广告" not in text  # 原始显示名不得泄露
    assert "tg://user?id=123456" in text  # 保留可点击链接（基于可信 ID）
    assert "123456</a>" not in text  # 链接文本不再是纯数字 ID
    assert "CAS 黑名单" in text
    assert "违规 3 次" in text

    # 用户状态通知（诈骗账号）
    text_scam = middleware._get_notification_text(user, "user_status_scam", {"status": "scam"})
    assert "John Doe广告" not in text_scam
    assert "tg://user?id=123456" in text_scam
    assert "诈骗账号" in text_scam
