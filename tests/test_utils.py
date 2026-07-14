"""工具函数测试"""

import pytest


@pytest.mark.unit
def test_escape_html():
    """测试 HTML 转义函数"""
    from src.core.utils import escape_html

    # 基本转义
    assert (
        escape_html("<script>alert('xss')</script>")
        == "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
    )
    assert escape_html("Hello & World") == "Hello &amp; World"
    assert escape_html('Test "quotes"') == "Test &quot;quotes&quot;"

    # 空字符串
    assert escape_html("") == ""
    assert escape_html(None) == ""

    # 正常文本
    assert escape_html("Hello World") == "Hello World"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (None, ""),
        ("", ""),
        (" \t\n ", ""),
        ("张", "*"),
        ("张三", "**"),
        ("张三李", "张*李"),
        ("张三李四", "张**四"),
        ("张三李四王", "张三*四王"),
        ("张三李四王五", "张三**王五"),
        ("A😀B", "A*B"),
        ("  Alice\tBob\n", "Al*****ob"),
    ],
)
def test_mask_user_name(name, expected):
    """测试用户名脱敏规则与空白规范化"""
    from src.core.utils import mask_user_name

    assert mask_user_name(name) == expected


@pytest.mark.unit
def test_mask_user_name_before_escape_html():
    """验证「先脱敏后转义」顺序：HTML 实体不会被破坏"""
    from src.core.utils import escape_html, mask_user_name

    masked = mask_user_name("<&X>Z")
    assert masked == "<&*>Z"
    assert escape_html(masked) == "&lt;&amp;*&gt;Z"


@pytest.mark.unit
def test_masked_mention_html():
    """测试脱敏的可点击 HTML 用户提及（保留 ID 链接，仅显示名脱敏）"""
    from src.core.utils import masked_mention_html

    class MockUser:
        def __init__(self, user_id, first_name, full_name=None, username=None):
            self.id = user_id
            self.first_name = first_name
            self.full_name = full_name
            self.username = username

    # 普通用户：链接基于可信数字 ID，显示名脱敏
    user = MockUser(123456, "John", "John Doe", "johndoe")
    assert masked_mention_html(user) == '<a href="tg://user?id=123456">Jo****oe</a>'

    # 含 HTML 特殊字符：脱敏后再转义，原始内容不得出现
    user2 = MockUser(789, "T", "<&X>Z", None)
    assert masked_mention_html(user2) == '<a href="tg://user?id=789">&lt;&amp;*&gt;Z</a>'


@pytest.mark.unit
def test_format_user_mention():
    """测试用户提及格式化（显示名与 @username 均脱敏）"""
    from src.core.utils import format_user_mention

    # 创建模拟用户对象
    class MockUser:
        def __init__(self, user_id, first_name, full_name=None, username=None):
            self.id = user_id
            self.first_name = first_name
            self.full_name = full_name
            self.username = username

    # 有 username 的用户：显示名与 @username 都应脱敏
    user1 = MockUser(123456, "John", "John Doe", "johndoe")
    result1 = format_user_mention(user1)
    assert result1 == "Jo****oe (@jo***oe)"
    assert "John Doe" not in result1  # 原始显示名不得泄露
    assert "@johndoe" not in result1  # 原始 @username 不得泄露

    # 没有 username 的用户：回退到数字 ID（ID 不脱敏）
    user2 = MockUser(789012, "Jane", "Jane Smith", None)
    result2 = format_user_mention(user2)
    assert result2 == "Ja******th (ID:789012)"
    assert "Jane Smith" not in result2

    # 带有 HTML 特殊字符的名字：脱敏后再转义，原始标签不得出现
    user3 = MockUser(111222, "Test", "<script>alert('xss')</script>", "test")
    result3 = format_user_mention(user3)
    assert "<script>" not in result3
    assert "&lt;" in result3  # HTML 已转义
    assert "alert" not in result3  # 中间内容被遮盖


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
    assert (
        parse_time_to_seconds("forever") is None or parse_time_to_seconds("forever") > 365 * 86400
    )

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
