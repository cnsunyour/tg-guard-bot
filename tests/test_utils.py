"""工具函数测试"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
        ("张三李四王", "张***王"),
        ("张三李四王五", "张****五"),
        ("A😀B", "A*B"),
        ("  Alice\tBob\n", "A*******b"),
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
    assert masked == "<***Z"
    assert escape_html(masked) == "&lt;***Z"


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
    assert masked_mention_html(user) == '<a href="tg://user?id=123456">J******e</a>'

    # 含 HTML 特殊字符：脱敏后再转义，原始内容不得出现
    user2 = MockUser(789, "T", "<&X>Z", None)
    assert masked_mention_html(user2) == '<a href="tg://user?id=789">&lt;***Z</a>'


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
    assert result1 == "J******e (@j*****e)"
    assert "John Doe" not in result1  # 原始显示名不得泄露
    assert "@johndoe" not in result1  # 原始 @username 不得泄露

    # 没有 username 的用户：回退到数字 ID（ID 不脱敏）
    user2 = MockUser(789012, "Jane", "Jane Smith", None)
    result2 = format_user_mention(user2)
    assert result2 == "J********h (ID:789012)"
    assert "Jane Smith" not in result2

    # 带有 HTML 特殊字符的名字：脱敏后再转义，原始标签不得出现
    user3 = MockUser(111222, "Test", "<script>alert('xss')</script>", "test")
    result3 = format_user_mention(user3)
    assert "<script>" not in result3
    assert "&lt;" in result3  # HTML 已转义
    assert "alert" not in result3  # 中间内容被遮盖


@pytest.mark.unit
def test_format_trusted_user_mention():
    """测试可信用户（管理员/操作者）提及格式化：完整显示名称但仍转义 HTML

    与 :func:`format_user_mention` 对称，区别在于显示名与 @username
    **不脱敏**（管理员/操作者名称需完整可见），但 HTML 特殊字符仍转义，
    防止注入与格式破坏。
    """
    from src.core.utils import format_trusted_user_mention

    class MockUser:
        def __init__(self, user_id, first_name, full_name=None, username=None):
            self.id = user_id
            self.first_name = first_name
            self.full_name = full_name
            self.username = username

    # 有 username 的可信用户：显示名与 @username 完整显示（不脱敏）
    admin1 = MockUser(123456, "John", "John Doe", "johndoe")
    result1 = format_trusted_user_mention(admin1)
    assert result1 == "John Doe (@johndoe)"

    # 无 username 的可信用户：完整名 + 数字 ID（ID 无需脱敏）
    admin2 = MockUser(789012, "Jane", "Jane Smith", None)
    result2 = format_trusted_user_mention(admin2)
    assert result2 == "Jane Smith (ID:789012)"

    # HTML 特殊字符的名字：完整显示但必须转义，禁止原样标签注入
    admin3 = MockUser(111222, "Test", "<script>alert('xss')</script>", "test")
    result3 = format_trusted_user_mention(admin3)
    assert "<script>" not in result3
    assert "&lt;script&gt;" in result3  # HTML 已转义，名称内容保留


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

    # L3: 尾部多余字符 / 0 时长 / 缺单位 → ValueError（不得误判永久或部分解析）
    for bad in ("30mxxx", "0m", "0", "abc", ""):
        with pytest.raises(ValueError):
            parse_time_to_seconds(bad)


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


@pytest.mark.unit
def test_get_app_version_matches_pyproject():
    """get_app_version 应返回 pyproject.toml 中定义的版本号

    版本号的唯一来源是 pyproject.toml，函数直接读取它，二者必须一致。
    本测试用于防止版本号被重新硬编码或读取逻辑失效。
    """
    import tomllib
    from pathlib import Path

    from src.core.utils import get_app_version

    # 测试文件位于 tests/ 下，项目根为其上一级目录
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        expected = tomllib.load(f)["project"]["version"]

    assert get_app_version() == expected


@pytest.mark.unit
def test_get_app_version_prefers_pyproject_over_metadata(monkeypatch):
    """pyproject.toml 版本应优先于包元数据

    锁定「pyproject 优先」这一优先级：mock 元数据返回一个明显的假版本，
    若函数错误地优先使用元数据则会命中该假版本。
    """
    import src.core.utils as utils

    monkeypatch.setattr(utils.importlib.metadata, "version", lambda _: "9.9.9-fake")
    assert utils.get_app_version() != "9.9.9-fake"


@pytest.mark.unit
def test_get_app_version_falls_back_to_metadata(monkeypatch):
    """pyproject.toml 不可用时回退到包元数据"""
    import src.core.utils as utils

    monkeypatch.setattr(utils, "_read_version_from_pyproject", lambda _: None)
    monkeypatch.setattr(utils.importlib.metadata, "version", lambda _: "2.0.0")
    assert utils.get_app_version() == "2.0.0"


@pytest.mark.unit
def test_get_app_version_unknown_when_both_unavailable(monkeypatch):
    """pyproject 与包元数据都不可用时返回 unknown"""
    from unittest.mock import Mock

    import src.core.utils as utils

    monkeypatch.setattr(utils, "_read_version_from_pyproject", lambda _: None)
    monkeypatch.setattr(
        utils.importlib.metadata,
        "version",
        Mock(side_effect=utils.importlib.metadata.PackageNotFoundError),
    )
    assert utils.get_app_version() == "unknown"


@pytest.mark.unit
def test_read_version_rejects_mismatched_project_name(tmp_path):
    """pyproject.toml 的 project.name 非 tg-guard-bot 时应回退（返回 None）"""
    from src.core.utils import _read_version_from_pyproject

    other = tmp_path / "pyproject.toml"
    other.write_text('[project]\nname = "other-bot"\nversion = "3.3.3"\n', encoding="utf-8")
    assert _read_version_from_pyproject(other) is None


@pytest.mark.unit
async def test_check_admin_permission_by_id_super_admin_skips_cache(mocker) -> None:
    """超管 id 直通，不查缓存"""
    from src.core.utils import check_admin_permission_by_id

    mocker.patch("src.core.config.settings.admin_ids", [9001])
    cache_check = mocker.patch(
        "src.core.cache.PermissionCache.is_admin", new=AsyncMock(return_value=False)
    )

    assert await check_admin_permission_by_id(MagicMock(), -100, 9001) is True
    cache_check.assert_not_awaited()


@pytest.mark.unit
async def test_check_admin_permission_by_id_delegates_regular_user(mocker) -> None:
    """普通用户委托 PermissionCache"""
    from src.core.utils import check_admin_permission_by_id

    mocker.patch("src.core.config.settings.admin_ids", [])
    cache_check = mocker.patch(
        "src.core.cache.PermissionCache.is_admin", new=AsyncMock(return_value=True)
    )
    bot = MagicMock()

    assert await check_admin_permission_by_id(bot, -100, 42) is True
    cache_check.assert_awaited_once_with(bot, -100, 42)


@pytest.mark.unit
async def test_check_admin_permission_anonymous_short_circuits_by_id(mocker) -> None:
    """匿名管理员（sender_chat==chat）直通，不调 by_id"""
    import src.core.utils as utils

    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100),
        sender_chat=SimpleNamespace(id=-100),
        from_user=SimpleNamespace(id=1087968824),
        text="anonymous message",
    )
    by_id = mocker.patch.object(
        utils, "check_admin_permission_by_id", new=AsyncMock(return_value=False)
    )

    assert await utils.check_admin_permission(message, MagicMock()) is True
    by_id.assert_not_awaited()


@pytest.mark.unit
async def test_check_admin_permission_without_from_user_fails_closed(mocker) -> None:
    """无 from_user 时 fail-closed（即使 sender_chat==chat.id 也不判管理员）"""
    import src.core.utils as utils

    by_id = mocker.patch.object(
        utils, "check_admin_permission_by_id", new=AsyncMock(return_value=True)
    )

    for sender_chat in (None, SimpleNamespace(id=-100)):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-100),
            sender_chat=sender_chat,
            from_user=None,
            text=None,
        )
        assert await utils.check_admin_permission(message, MagicMock()) is False

    by_id.assert_not_awaited()


# ---------- 管理员权限过滤（spam 提示只 mention 能处置违规者）----------


def _admin(user_id: int, *, anonymous: bool = False, delete: bool = True, restrict: bool = True):
    """构造一个非匿名/具名权限的普通管理员（ChatMemberAdministrator）"""
    from aiogram.types import ChatMemberAdministrator, User

    return ChatMemberAdministrator(
        user=User(id=user_id, is_bot=False, first_name=f"u{user_id}"),
        is_anonymous=anonymous,
        can_be_edited=False,
        can_manage_chat=delete or restrict,
        can_delete_messages=delete,
        can_manage_video_chats=False,
        can_restrict_members=restrict,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )


def _owner(user_id: int, *, anonymous: bool = False):
    """构造群主（ChatMemberOwner）——隐含全部权限，无 can_delete/restrict 字段"""
    from aiogram.types import ChatMemberOwner, User

    return ChatMemberOwner(
        user=User(id=user_id, is_bot=False, first_name=f"owner{user_id}"),
        is_anonymous=anonymous,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("admin_factory", "expected"),
    [
        # 群主：非匿名始终计入，匿名排除
        (lambda: _owner(1, anonymous=False), True),
        (lambda: _owner(2, anonymous=True), False),
        # 普通管理员：需同时具备删除 + 封禁两项权限
        (lambda: _admin(3, delete=True, restrict=True), True),
        (lambda: _admin(4, delete=True, restrict=False), False),
        (lambda: _admin(5, delete=False, restrict=True), False),
        (lambda: _admin(6, delete=False, restrict=False), False),
        # 匿名管理员一律排除（即便权限齐全）
        (lambda: _admin(7, anonymous=True, delete=True, restrict=True), False),
    ],
)
def test_can_handle_spam_filter_rule(admin_factory, expected) -> None:
    """_can_handle_spam：群主始终计入、普通管理员需双权限、匿名一律排除"""
    from src.core.utils import _can_handle_spam

    assert _can_handle_spam(admin_factory()) is expected


@pytest.mark.unit
async def test_get_spam_handler_admins_mention_caches_filtered_ids(mocker) -> None:
    """API 路径：过滤后 ID 列表写入缓存，空列表也缓存（省后续 API 调用）"""
    import src.core.utils as utils

    redis = SimpleNamespace(
        get=AsyncMock(return_value=None),
        setex=AsyncMock(),
    )
    mocker.patch.object(utils, "get_redis", return_value=redis)

    bot = MagicMock()
    bot.get_chat_administrators = AsyncMock(
        return_value=[
            _owner(100, anonymous=False),  # 群主：计入
            _admin(200, delete=True, restrict=True),  # 双权限：计入
            _admin(300, delete=True, restrict=False),  # 缺封禁权：排除
            _owner(400, anonymous=True),  # 匿名群主：排除
        ]
    )

    mentions = await utils.get_spam_handler_admins_mention(bot, -100123)

    # 仅群主 100 与双权限管理员 200 进入 mention
    assert "id=100" in mentions and "id=200" in mentions
    assert "id=300" not in mentions and "id=400" not in mentions
    # 过滤后 ID 列表已缓存（非空也写）
    cached = redis.setex.await_args
    assert cached.args[1] == 300  # TTL 5 分钟
    assert cached.args[2] == json.dumps([{"id": 100}, {"id": 200}], ensure_ascii=False)


@pytest.mark.unit
async def test_get_spam_handler_admins_mention_caches_empty_when_none_eligible(mocker) -> None:
    """无符合条件管理员时缓存空列表，避免反复请求 Telegram API"""
    import src.core.utils as utils

    redis = SimpleNamespace(get=AsyncMock(return_value=None), setex=AsyncMock())
    mocker.patch.object(utils, "get_redis", return_value=redis)
    bot = MagicMock()
    bot.get_chat_administrators = AsyncMock(
        return_value=[_admin(500, delete=False, restrict=False)]
    )

    mentions = await utils.get_spam_handler_admins_mention(bot, -100456)

    assert mentions == ""
    cached = redis.setex.await_args
    assert cached.args[2] == json.dumps([], ensure_ascii=False)


@pytest.mark.unit
async def test_get_spam_handler_admins_mention_cache_hit_skips_api(mocker) -> None:
    """缓存命中直接渲染，不调用 Telegram API"""
    import src.core.utils as utils

    redis = SimpleNamespace(
        get=AsyncMock(return_value=json.dumps([{"id": 700}, {"id": 800}], ensure_ascii=False)),
        setex=AsyncMock(),
    )
    mocker.patch.object(utils, "get_redis", return_value=redis)
    bot = MagicMock()
    bot.get_chat_administrators = AsyncMock()

    mentions = await utils.get_spam_handler_admins_mention(bot, -100789)

    assert "id=700" in mentions and "id=800" in mentions
    bot.get_chat_administrators.assert_not_awaited()
    redis.setex.assert_not_awaited()


@pytest.mark.unit
async def test_get_spam_handler_admins_mention_api_failure_returns_empty(mocker) -> None:
    """Telegram API 异常时降级返回空 mention（调用方据此去掉 🔔 前缀）"""
    import src.core.utils as utils

    redis = SimpleNamespace(get=AsyncMock(return_value=None), setex=AsyncMock())
    mocker.patch.object(utils, "get_redis", return_value=redis)
    bot = MagicMock()
    bot.get_chat_administrators = AsyncMock(side_effect=RuntimeError("telegram down"))

    assert await utils.get_spam_handler_admins_mention(bot, -100999) == ""
    redis.setex.assert_not_awaited()  # API 失败不写缓存
