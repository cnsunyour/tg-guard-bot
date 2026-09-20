"""跨聊天回复（external_reply）检测与处置测试。

覆盖 ``check_and_handle_external_reply``：
- 快速路径（无 external_reply 不查 DB）
- 群开关（关闭跳过 / group None 默认启用）
- 论坛同群豁免（forum topic 场景 external_reply.chat 即本群）
- 关联频道豁免（缓存命中 / miss 回填 / get_chat 异常 fail-punish）
- 四种 origin 统一命中 → 专用处置（不经确认模式 / 投票路由）
- 内部异常吞咽（return False 绝不 return True）

覆盖 ``_handle_external_reply_hit``：
- 删除 + 系统警告（稳定 reason code）+ 群内提示（含累计次数行）+ 自动删除
- 删除失败仍记警告（消息处置与账号处置独立）
- 警告失败提示不带累计行
- 不入训练库、不缓存文本、不走检测路由
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import antispam

pytestmark = pytest.mark.unit

CHAT_ID = -100123
USER_ID = 42
BOT_ID = 999


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


def _message(*, external: SimpleNamespace | None, text: str | None = "无意义正文") -> MagicMock:
    """构造 Message mock（external 为 None 时表示普通消息）。"""
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID, type="supergroup", title="Test")
    message.external_reply = external
    message.quote = None
    message.text = text
    message.caption = None
    message.from_user = SimpleNamespace(
        id=USER_ID, username=None, full_name="Spammer", first_name="Spammer"
    )
    message.message_id = 5
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=MagicMock(name="notice"))
    return message


def _stub_detection(
    mocker,
    *,
    enabled: bool = True,
    group_exists: bool = True,
    redis_cached: str | None = None,
    linked_chat_id: int | None = None,
    get_chat_error: Exception | None = None,
):
    """打桩检测函数的外部依赖，返回 (hit, redis_mock, bot)。"""
    group = SimpleNamespace(anti_external_reply_enabled=enabled) if group_exists else None
    mocker.patch.object(antispam.GroupRepository, "get", new=AsyncMock(return_value=group))
    hit = mocker.patch.object(antispam, "_handle_external_reply_hit", new=AsyncMock())

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
    return hit, redis_mock, bot


def _stub_handling(mocker, *, warn_result: tuple[bool, int, bool] = (True, 2, False)):
    """打桩处置函数的外部依赖，返回 (warn_user, localizer, auto_delete, bot)。"""
    warn_user = mocker.patch.object(
        antispam.ModerationService, "warn_user", new=AsyncMock(return_value=warn_result)
    )
    localizer = MagicMock()
    localizer.t.side_effect = lambda key, **kw: f"<{key}:{kw}>" if kw else f"<{key}>"
    mocker.patch.object(antispam, "get_resolver")
    antispam.get_resolver.return_value.for_group = AsyncMock(return_value="zh-Hans")
    mocker.patch.object(
        antispam,
        "get_translator",
        return_value=MagicMock(for_locale=MagicMock(return_value=localizer)),
    )
    auto_delete = mocker.patch.object(antispam, "auto_delete_message", new=AsyncMock())
    bot = MagicMock(id=BOT_ID)
    return warn_user, localizer, auto_delete, bot


# ===== 检测：快速路径与群开关 =====


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
    hit, _, bot = _stub_detection(mocker, enabled=False)
    message = _message(external=_external())

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    hit.assert_not_awaited()


async def test_group_none_defaults_enabled(mocker) -> None:
    """未建组（group None）→ 按默认启用处理 → 命中处置"""
    hit, _, bot = _stub_detection(mocker, group_exists=False)
    message = _message(external=_external())

    assert await antispam.check_and_handle_external_reply(message, bot) is True
    hit.assert_awaited_once_with(message, bot)


# ===== 检测：豁免（论坛同群 / 关联频道）=====


async def test_forum_topic_same_chat_exempt(mocker) -> None:
    """论坛跨 topic 回复（external_reply.chat 即本群）→ False，不处置"""
    hit, _, bot = _stub_detection(mocker)
    message = _message(external=_external(source_chat_id=CHAT_ID))

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    hit.assert_not_awaited()


async def test_linked_channel_exempt_with_cache_hit(mocker) -> None:
    """关联频道豁免（Redis 缓存命中）→ False，不调 get_chat"""
    hit, redis_mock, bot = _stub_detection(mocker, redis_cached="-100999")
    message = _message(external=_external(source_chat_id=-100999))

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    redis_mock.get.assert_awaited_once()
    bot.get_chat.assert_not_awaited()
    hit.assert_not_awaited()


async def test_linked_channel_exempt_with_cache_backfill(mocker) -> None:
    """缓存 miss → get_chat 回填缓存后豁免 → False"""
    hit, redis_mock, bot = _stub_detection(mocker, redis_cached=None, linked_chat_id=-100999)
    message = _message(external=_external(source_chat_id=-100999))

    assert await antispam.check_and_handle_external_reply(message, bot) is False
    bot.get_chat.assert_awaited_once()
    redis_mock.set.assert_awaited_once()  # 回填缓存
    hit.assert_not_awaited()


async def test_linked_channel_cache_none_sentinel(mocker) -> None:
    """缓存空串哨兵（无关联频道）→ 不调 get_chat，继续命中"""
    hit, _, bot = _stub_detection(mocker, redis_cached="")
    message = _message(external=_external(source_chat_id=-100999))

    assert await antispam.check_and_handle_external_reply(message, bot) is True
    bot.get_chat.assert_not_awaited()
    hit.assert_awaited_once()


async def test_get_chat_failure_fails_punished(mocker) -> None:
    """get_chat 异常 → fail-punish（按无关联频道继续处置）"""
    hit, _, bot = _stub_detection(mocker, get_chat_error=RuntimeError("flood"))
    message = _message(external=_external(source_chat_id=-100888))

    assert await antispam.check_and_handle_external_reply(message, bot) is True
    hit.assert_awaited_once()


# ===== 检测：四种 origin 统一命中，不经检测路由 =====


@pytest.mark.parametrize("origin_type", ["channel", "user", "hidden_user", "chat"])
async def test_all_origins_unified_hit(mocker, origin_type: str) -> None:
    """四种 origin 统一命中（用户决策：不做 origin 分级）"""
    hit, _, bot = _stub_detection(mocker)
    message = _message(external=_external(origin_type=origin_type, source_chat_id=None))

    assert await antispam.check_and_handle_external_reply(message, bot) is True
    hit.assert_awaited_once_with(message, bot)


async def test_hit_bypasses_spam_detection_routing(mocker) -> None:
    """命中不走 _route_spam_detection（不进确认模式复核 / 集体投票）"""
    _, _, bot = _stub_detection(mocker)
    route = mocker.patch.object(antispam, "_route_spam_detection", new=AsyncMock())
    message = _message(external=_external())

    await antispam.check_and_handle_external_reply(message, bot)

    route.assert_not_awaited()


# ===== 检测：异常吞咽 =====


async def test_internal_exception_returns_false(mocker) -> None:
    """内部异常（处置抛错）→ 吞咽返回 False，绝不静默放走后误标已处理"""
    hit, _, bot = _stub_detection(mocker)
    hit.side_effect = RuntimeError("handle boom")
    message = _message(external=_external())

    assert await antispam.check_and_handle_external_reply(message, bot) is False


# ===== 处置：删除 + 系统警告 + 群内提示 =====


async def test_hit_deletes_warns_and_notifies(mocker) -> None:
    """删除消息 → 稳定 reason code 记警告 → 提示含脱敏 mention 与累计次数行 → 自动删除"""
    warn_user, localizer, auto_delete, bot = _stub_handling(mocker, warn_result=(True, 2, False))
    message = _message(external=_external())

    await antispam._handle_external_reply_hit(message, bot)

    message.delete.assert_awaited_once()
    warn_user.assert_awaited_once_with(
        bot=bot,
        chat_id=CHAT_ID,
        user_id=USER_ID,
        operator_id=BOT_ID,
        reason="system:external_reply",
    )
    localizer.t.assert_any_call("antispam.external_reply.warning.line", warning_count=2)
    deleted_call = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("antispam.external_reply.deleted.message",)
    )
    assert deleted_call.kwargs["warning_line"].startswith("<antispam.external_reply.warning.line")
    assert "Spammer" not in deleted_call.kwargs["user"]  # format_user_mention 脱敏
    message.answer.assert_awaited_once()
    assert message.answer.await_args.kwargs["parse_mode"] == "HTML"
    auto_delete.assert_awaited_once_with(message.answer.return_value, delay=30)


async def test_hit_delete_failure_still_warns(mocker) -> None:
    """删除失败（权限缺失等）不阻断记警告与提示"""
    warn_user, _, _, bot = _stub_handling(mocker)
    message = _message(external=_external())
    message.delete = AsyncMock(side_effect=RuntimeError("no rights"))

    await antispam._handle_external_reply_hit(message, bot)

    warn_user.assert_awaited_once()
    message.answer.assert_awaited_once()


async def test_hit_warn_failure_omits_warning_line(mocker) -> None:
    """warn_user 返回失败 → 提示不带累计次数行（不展示错误的 0 次）"""
    _, localizer, _, bot = _stub_handling(mocker, warn_result=(False, 0, False))
    message = _message(external=_external())

    await antispam._handle_external_reply_hit(message, bot)

    deleted_call = next(
        c
        for c in localizer.t.call_args_list
        if c.args == ("antispam.external_reply.deleted.message",)
    )
    assert deleted_call.kwargs["warning_line"] == ""
    assert not any(
        c.args == ("antispam.external_reply.warning.line",) for c in localizer.t.call_args_list
    )


async def test_hit_never_trains_or_caches(mocker) -> None:
    """结构信号检测不入训练库、不缓存 spam_text（正文是无意义占位，入库污染分类器）"""
    _, _, _, bot = _stub_handling(mocker)
    detector = MagicMock(add_feedback=AsyncMock())
    mocker.patch.object(antispam, "get_detector", return_value=detector)
    redis_mock = MagicMock(setex=AsyncMock())
    mocker.patch.object(antispam, "get_redis", return_value=redis_mock)
    message = _message(external=_external())

    await antispam._handle_external_reply_hit(message, bot)

    detector.add_feedback.assert_not_awaited()
    redis_mock.setex.assert_not_awaited()


async def test_hit_without_from_user_only_deletes(mocker) -> None:
    """无 from_user（防御外部直接调用）→ 仅删除，不记警告、不发提示"""
    warn_user, _, _, bot = _stub_handling(mocker)
    message = _message(external=_external())
    message.from_user = None

    await antispam._handle_external_reply_hit(message, bot)

    message.delete.assert_awaited_once()
    warn_user.assert_not_awaited()
    message.answer.assert_not_awaited()
