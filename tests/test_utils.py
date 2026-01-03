"""工具函数测试"""

import pytest


@pytest.mark.unit
def test_escape_html():
    """测试 HTML 转义函数"""
    from src.core.utils import escape_html

    # 基本转义
    assert escape_html("<script>alert('xss')</script>") == "&lt;script&gt;alert('xss')&lt;/script&gt;"
    assert escape_html("Hello & World") == "Hello &amp; World"
    assert escape_html('Test "quotes"') == "Test &quot;quotes&quot;"

    # 空字符串
    assert escape_html("") == ""
    assert escape_html(None) == ""

    # 正常文本
    assert escape_html("Hello World") == "Hello World"


@pytest.mark.unit
def test_format_user_mention():
    """测试用户提及格式化"""
    from src.core.utils import format_user_mention

    # 创建模拟用户对象
    class MockUser:
        def __init__(self, user_id, first_name, full_name=None, username=None):
            self.id = user_id
            self.first_name = first_name
            self.full_name = full_name
            self.username = username

    # 有 username 的用户
    user1 = MockUser(123456, "John", "John Doe", "johndoe")
    result1 = format_user_mention(user1)
    assert "John Doe" in result1
    assert "@johndoe" in result1
    assert "<" not in result1  # 确保 HTML 被转义

    # 没有 username 的用户
    user2 = MockUser(789012, "Jane", "Jane Smith", None)
    result2 = format_user_mention(user2)
    assert "Jane Smith" in result2
    assert "ID:789012" in result2

    # 带有特殊字符的名字
    user3 = MockUser(111222, "Test", "<script>alert('xss')</script>", "test")
    result3 = format_user_mention(user3)
    assert "&lt;script&gt;" in result3  # HTML 应该被转义
    assert "<script>" not in result3


@pytest.mark.unit
def test_parse_time_to_seconds():
    """测试时间解析函数"""
    from src.core.utils import parse_time_to_seconds

    # 分钟
    assert parse_time_to_seconds("30m") == 30 * 60
    assert parse_time_to_seconds("1m") == 60

    # 小时
    assert parse_time_to_seconds("2h") == 2 * 3600
    assert parse_time_to_seconds("24h") == 24 * 3600

    # 天
    assert parse_time_to_seconds("1d") == 86400
    assert parse_time_to_seconds("7d") == 7 * 86400

    # 永久（None 或大数值）
    assert parse_time_to_seconds("forever") is None or parse_time_to_seconds("forever") > 365 * 86400

    # 无效输入
    with pytest.raises((ValueError, AttributeError)):
        parse_time_to_seconds("invalid")


@pytest.mark.unit
def test_mask_text():
    """测试文本脱敏函数"""
    from src.core.utils import mask_text

    # 短文本
    assert mask_text("short") == "***"

    # 长文本
    result = mask_text("This is a very long text that should be masked")
    assert result.startswith("This is a ")
    assert "***" in result
    assert "length:" in result

    # 自定义长度
    result2 = mask_text("1234567890abcdefghij", show_length=5)
    assert result2.startswith("12345")
    assert "***" in result2


@pytest.mark.unit
def test_validate_user_id():
    """测试用户 ID 验证"""
    from src.core.utils import validate_user_id

    # 有效的用户 ID
    assert validate_user_id(123456789) is True
    assert validate_user_id(1) is True

    # 无效的用户 ID（负数、0、超大值）
    assert validate_user_id(-1) is False
    assert validate_user_id(0) is False
    assert validate_user_id(2**63) is False  # 超出范围
