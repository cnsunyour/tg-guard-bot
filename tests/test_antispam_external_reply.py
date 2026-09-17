"""跨聊天回复（external_reply）检测测试。

覆盖 ``check_and_handle_external_reply``：
- 快速路径（无 external_reply 不查 DB）
- 群开关（关闭跳过 / group None 默认启用）
- 论坛同群豁免（forum topic 场景 external_reply.chat 即本群）
- 关联频道豁免（缓存命中 / miss 回填 / get_chat 异常 fail-punish）
- 四种 origin 统一命中 + 合成 result 字段（sample_text 回退链 / confidence / reasons）
- 内部异常吞咽（return False 绝不 return True）
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam
from src.services.spam_review import SpamMessageType

pytestmark = pytest.mark.unit

CHAT_ID = -100123
USER_ID = 42


def _external(
    *,
    origin_type: str = "channel",
    source_chat_id: int | None = -100999,
    source_message_id: int | None = 777,
) -> SimpleNamespace:
    """构造 ExternalReplyInfo mock（chat 为 None 模拟 user/hidden_user origin）。"""
    return SimpleNamespace(
        chat=SimpleNamespace(id=source_chat_id) if source_chat_id is not None else None,
        message_id=source_message_id,
        # 对齐 aiogram 结构：origin.type 是 MessageOriginType（str 枚举），.value 取 str
        origin=SimpleNamespace(type=SimpleNamespace(value=origin_type)),
    )


def _message(
    *,
    external: SimpleNamespace | None,
    text: str | None = "无意义正文",
    caption: str | None = None,
    quote_text: str | None = None,
) -> MagicMock:
    """构造 Message mock（external 为 None 时表示普通消息）。"""
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID, type="supergroup", title="Test")
    message.external_reply = external
    message.quote = SimpleNamespace(text=quote_text) if quote_text is not None else None
    message.text = text
    message.caption = caption
    message.from_user = SimpleNamespace(id=USER_ID, username=None)
    message.message_id = 5
    return message


def _stub(
    mocker,
    *,
    enabled: bool = True,
    group_exists: bool = True,
    redis_cached: str | None = None,
    linked_chat_id: int | None = None,
    get_chat_error: Exception | None = None,
):
    """打桩检测函数的外部依赖，返回 (route, redis_mock, get_chat)。"""
    group = SimpleNamespace(anti_external_reply_enabled=enabled) if group_exists else None
    mocker.patch.object(antispam.GroupRepository, "get", new=AsyncMock(return_value=group))
    route = mocker.patch.object(antispam, "_route_spam_detection", new=AsyncMock())

    redis_mock = MagicMock()
    redis_mock.get = AsyncMock(return_value=redis_cached)
    redis_mock.set = AsyncMock()
    mocker.patch.object(antispam, "get_redis", new=MagicMock(return_value=redis_mock))

    chat_info = MagicMock()
    chat_info.linked_chat_id = linked_chat_id
    bot = MagicMock()
    bot.get_chat = AsyncMock(return_value=chat_info)
    if get_chat_error is not None:
        bot.get_chat = AsyncMock(side_effect=get_chat_error)
    return route, redis_mock, bot


# ===== 快速路径与群开关 =====


async def test_no_external_reply_skips_without_db(mocker) -> None:
    """无 external_reply → False，且不查群组配置（零成本快速路径）"""
    repo_get = mocker.patch.object(
        antispam.GroupRepository, "get", new=AsyncMock(return_value=None)
    )
    message = _message(external=None)

    assert await antispam.check_and_handle_external_reply(message, MagicMock()) is False
    repo_get.assert_not_awaited()


async def test_disabled_group_skips(mocker) -> None:
    """群开关关闭 → False（豁免路径不占用 skip，回归正常管线）"""
    route, _, bot = _stub(mocker, enabled=False)
    message = _message(external=_external())

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    route.assert_not_awaited()


async def test_group_none_defaults_enabled(mocker) -> None:
    """未建组（group None）→ 按默认启用处理 → 命中路由"""
    route, _, bot = _stub(mocker, group_exists=False)
    message = _message(external=_external())

    assert await antispam.check_and_handle_external_reply(message, bot) is True
    route.assert_awaited_once()


# ===== 豁免：论坛同群 / 关联频道 =====


async def test_forum_topic_same_chat_exempt(mocker) -> None:
    """论坛跨 topic 回复（external_reply.chat 即本群）→ False，不路由"""
    route, _, bot = _stub(mocker)
    message = _message(external=_external(source_chat_id=CHAT_ID))

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    route.assert_not_awaited()


async def test_linked_channel_exempt_with_cache_hit(mocker) -> None:
    """关联频道豁免（Redis 缓存命中）→ False，不调 get_chat"""
    route, redis_mock, bot = _stub(mocker, redis_cached="-100999")
    message = _message(external=_external(source_chat_id=-100999))

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    redis_mock.get.assert_awaited_once()
    bot.get_chat.assert_not_awaited()
    route.assert_not_awaited()


async def test_linked_channel_exempt_with_cache_backfill(mocker) -> None:
    """缓存 miss → get_chat 回填缓存后豁免 → False"""
    route, redis_mock, bot = _stub(mocker, redis_cached=None, linked_chat_id=-100999)
    message = _message(external=_external(source_chat_id=-100999))

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    bot.get_chat.assert_awaited_once()
    redis_mock.set.assert_awaited_once()  # 回填缓存
    route.assert_not_awaited()


async def test_linked_channel_cache_none_sentinel(mocker) -> None:
    """缓存空串哨兵（无关联频道）→ 不调 get_chat，继续命中检测"""
    route, _, bot = _stub(mocker, redis_cached="")
    message = _message(external=_external(source_chat_id=-100999))

    assert await antispam.check_and_handle_external_reply(message, bot) is True
    bot.get_chat.assert_not_awaited()
    route.assert_awaited_once()


async def test_get_chat_failure_fails_punished(mocker) -> None:
    """get_chat 异常 → fail-punish（按无关联频道继续检测，与频道马甲先例一致）"""
    route, _, bot = _stub(mocker, get_chat_error=RuntimeError("flood"))
    message = _message(external=_external(source_chat_id=-100888))

    assert await antispam.check_and_handle_external_reply(message, bot) is True
    route.assert_awaited_once()


# ===== 命中：四种 origin 统一对待 + 合成 result =====


@pytest.mark.parametrize("origin_type", ["channel", "user", "hidden_user", "chat"])
async def test_all_origins_unified_hit(mocker, origin_type: str) -> None:
    """四种 origin 统一命中（用户决策：不做 origin 分级）"""
    route, _, bot = _stub(mocker)
    external = _external(origin_type=origin_type, source_chat_id=None)
    message = _message(external=external)

    assert await antispam.check_and_handle_external_reply(message, bot) is True

    result = route.await_args.args[2]
    assert result["is_spam"] is True
    assert result["stage"] == "external_reply"
    assert result["confidence"] == 0.9
    assert result["reasons"] == ["external_reply"]
    assert result["details"]["origin_type"] == origin_type
    assert route.await_args.kwargs["message_type"] is SpamMessageType.external_reply


async def test_sample_text_fallback_chain(mocker) -> None:
    """sample_text 回退链：正文 → caption → quote 截断 → 空串"""
    route, _, bot = _stub(mocker)

    # 1. 正文优先
    message = _message(external=_external(), text="正文", caption="标题", quote_text="引用")
    await antispam.check_and_handle_external_reply(message, bot)
    assert route.await_args.args[2]["details"]["sample_text"] == "正文"

    # 2. 无正文用 caption
    message = _message(external=_external(), text=None, caption="标题", quote_text="引用")
    await antispam.check_and_handle_external_reply(message, bot)
    assert route.await_args.args[2]["details"]["sample_text"] == "标题"

    # 3. quote 兜底（截断 200）
    message = _message(external=_external(), text=None, caption=None, quote_text="引" * 300)
    await antispam.check_and_handle_external_reply(message, bot)
    assert route.await_args.args[2]["details"]["sample_text"] == "引" * 200

    # 4. 全空 → 空串（配合 add_feedback 空文本守卫不入训练库）
    message = _message(external=_external(), text=None, caption=None, quote_text=None)
    await antispam.check_and_handle_external_reply(message, bot)
    assert route.await_args.args[2]["details"]["sample_text"] == ""


async def test_details_carry_source_ids(mocker) -> None:
    """details 携带来源聊天/消息 ID（供日志与审计追溯）"""
    route, _, bot = _stub(mocker)
    message = _message(external=_external(source_chat_id=-100777, source_message_id=88))

    await antispam.check_and_handle_external_reply(message, bot)

    details = route.await_args.args[2]["details"]
    assert details["source_chat_id"] == -100777
    assert details["source_message_id"] == 88


# ===== 异常吞咽 =====


async def test_internal_exception_returns_false(mocker) -> None:
    """内部异常（_route 抛错）→ 吞咽返回 False，绝不静默放走后误标已处理"""
    route, _, bot = _stub(mocker)
    route.side_effect = RuntimeError("route boom")
    message = _message(external=_external())

    assert await antispam.check_and_handle_external_reply(message, bot) is False
