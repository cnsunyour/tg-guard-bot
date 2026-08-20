"""群内验证引导消息的匿名 mention 行为测试。

覆盖 handler 层编排：join flow 聚合发送 + 晚到用户编辑补全、join_request 全程不
mention、Redis 故障降级。Redis 状态机本身由 test_verification_hint.py 覆盖。

渲染使用真实 catalog（locales/zh-Hans.json）+ strict 模式，确保新增文案 key 与
占位符真实可用，而不是被 mock 掩盖。
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import verification as handler
from src.core.i18n.translator import Translator
from src.services.verification_hint import HintEditClaim

pytestmark = pytest.mark.unit

CHAT_ID = -1001234567890
USER_ID = 1001
LATE_USER_ID = 1002
MENTIONS_KEY = "verification.hint.join.group.mentions"


def _translator() -> Translator:
    """用真实中文 catalog 构造 strict translator（缺 key / 缺变量直接抛错）。"""
    catalog = json.loads(Path("locales/zh-Hans.json").read_text(encoding="utf-8"))
    return Translator({"zh-Hans": catalog}, default_locale="zh-Hans", strict=True)


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="guard_bot"))
    bot.get_chat = AsyncMock(return_value=MagicMock(title="测试群"))
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=4242))
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _patch_common(mocker) -> None:
    """mock 引导消息渲染所需的 locale / 定时依赖。"""
    resolver = AsyncMock()
    resolver.for_group.return_value = "zh-Hans"
    mocker.patch.object(handler, "get_resolver", return_value=resolver)
    mocker.patch.object(handler, "get_translator", return_value=_translator())
    # 聚合等待与延迟删除任务在单测中不真实执行
    mocker.patch.object(handler.asyncio, "sleep", new=AsyncMock())
    mocker.patch.object(handler.asyncio, "create_task", side_effect=lambda coro: coro.close())


def _sent_text(bot: MagicMock) -> str:
    return bot.send_message.await_args.kwargs["text"]


def _edited_text(bot: MagicMock) -> str:
    return bot.edit_message_text.await_args.kwargs["text"]


async def test_join_hint_mentions_all_waiting_users(mocker) -> None:
    """取得发送权者聚合等待后，把窗口内所有等待验证的用户一并 mention 进首条消息。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value="owner-token"))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(True, False, 1)))
    snapshot = mocker.patch.object(
        handler,
        "snapshot_hint_users",
        new=AsyncMock(return_value=([USER_ID, LATE_USER_ID], 2)),
    )
    mocker.patch.object(handler, "promote_hint", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "claim_hint_render", new=AsyncMock(return_value=True))
    # promote 后的补检：渲染内容未变化 → Redis 侧不发放编辑权，不产生多余编辑
    mocker.patch.object(handler, "claim_hint_edit", new=AsyncMock(return_value=None))

    await handler.handle_user_not_started_bot(bot, CHAT_ID, USER_ID)

    text = _sent_text(bot)
    assert text.startswith("🔔 ")
    assert f'<a href="tg://user?id={USER_ID}">👤</a>' in text
    assert f'<a href="tg://user?id={LATE_USER_ID}">👤</a>' in text
    # mention 行独立成段，原文案完整保留
    assert "\n\n⚠️ <b>入群验证提示</b>" in text
    # 聚合等待发生在快照之前
    handler.asyncio.sleep.assert_awaited_once_with(
        handler.settings.verification_hint_aggregation_delay
    )
    snapshot.assert_awaited_with(CHAT_ID, "join", handler.settings.verification_hint_max_mentions)
    bot.edit_message_text.assert_not_awaited()


async def test_join_hint_without_users_keeps_original_text(mocker) -> None:
    """名单为空时不得留下孤立的 🔔 或多余空行。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value="owner-token"))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(True, False, 0)))
    mocker.patch.object(handler, "snapshot_hint_users", new=AsyncMock(return_value=([], 0)))
    mocker.patch.object(handler, "promote_hint", new=AsyncMock(return_value=True))
    claim = mocker.patch.object(handler, "claim_hint_render", new=AsyncMock(return_value=False))

    await handler.handle_user_not_started_bot(bot, CHAT_ID, USER_ID)

    text = _sent_text(bot)
    assert text.startswith("⚠️ <b>入群验证提示</b>")
    assert "🔔" not in text
    assert "tg://user" not in text
    # 没有 mention 就没有渲染版本可提交
    claim.assert_not_awaited()


async def test_join_request_hint_never_mentions(mocker) -> None:
    """加入请求用户尚未入群、收不到群消息，该 flow 不参与 mention 状态机。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value="owner-token"))
    add_user = mocker.patch.object(handler, "add_hint_user", new=AsyncMock())
    snapshot = mocker.patch.object(handler, "snapshot_hint_users", new=AsyncMock())
    mocker.patch.object(handler, "promote_hint", new=AsyncMock(return_value=True))

    await handler.handle_user_not_started_bot_for_join_request(bot, CHAT_ID, USER_ID)

    text = _sent_text(bot)
    assert "tg://user" not in text
    assert "🔔" not in text
    add_user.assert_not_awaited()
    snapshot.assert_not_awaited()
    handler.asyncio.sleep.assert_not_awaited()


async def test_late_user_edits_committed_hint(mocker) -> None:
    """窗口内晚到的用户通过编辑补进已发出的消息（视觉补全）。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value=None))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(True, True, 2)))
    mocker.patch.object(handler, "try_extend_hint", new=AsyncMock(return_value=True))
    mocker.patch.object(
        handler,
        "claim_hint_edit",
        new=AsyncMock(
            return_value=HintEditClaim(
                message_id=4242, mention_ids=[USER_ID, LATE_USER_ID], total=2
            )
        ),
    )

    await handler.handle_user_not_started_bot(bot, CHAT_ID, LATE_USER_ID)

    bot.send_message.assert_not_awaited()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 4242
    text = _edited_text(bot)
    assert f'<a href="tg://user?id={USER_ID}">👤</a>' in text
    assert f'<a href="tg://user?id={LATE_USER_ID}">👤</a>' in text


async def test_late_user_skips_edit_when_render_version_not_advanced(mocker) -> None:
    """渲染内容不会变化时 Redis 侧不发放编辑权，避免多余编辑与旧内容覆盖。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value=None))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(True, True, 2)))
    mocker.patch.object(handler, "try_extend_hint", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "claim_hint_edit", new=AsyncMock(return_value=None))

    await handler.handle_user_not_started_bot(bot, CHAT_ID, LATE_USER_ID)

    bot.edit_message_text.assert_not_awaited()


async def test_pending_window_user_waits_for_owner_snapshot(mocker) -> None:
    """消息尚未发出时（owner 仍在聚合），晚到用户不编辑，由 owner 的快照带出。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value=None))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(True, False, 2)))
    mocker.patch.object(handler, "try_extend_hint", new=AsyncMock(return_value=False))
    claim = mocker.patch.object(handler, "claim_hint_edit", new=AsyncMock())

    await handler.handle_user_not_started_bot(bot, CHAT_ID, LATE_USER_ID)

    claim.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_redis_failure_on_extend_does_not_propagate(mocker) -> None:
    """延长共享窗口失败不得冒泡：异常会让 on_user_join 走封禁分支，误封正常用户。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value=None))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(False, False, 0)))
    mocker.patch.object(
        handler, "try_extend_hint", new=AsyncMock(side_effect=RuntimeError("redis down"))
    )

    await handler.handle_user_not_started_bot(bot, CHAT_ID, LATE_USER_ID)


async def test_redis_failure_on_reserve_skips_hint_without_raising(mocker) -> None:
    """竞争发送权失败同样只跳过引导消息，验证命运交给 timeout task 兜底。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(
        handler, "reserve_hint", new=AsyncMock(side_effect=RuntimeError("redis down"))
    )

    await handler.handle_user_not_started_bot(bot, CHAT_ID, USER_ID)

    bot.send_message.assert_not_awaited()


async def test_promote_rejection_deletes_unmanaged_message(mocker) -> None:
    """reservation 在发送期间失效时，已发出的消息不受状态机管理，必须立即删除。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value="owner-token"))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(True, False, 1)))
    mocker.patch.object(handler, "snapshot_hint_users", new=AsyncMock(return_value=([USER_ID], 1)))
    mocker.patch.object(handler, "promote_hint", new=AsyncMock(return_value=False))
    claim = mocker.patch.object(handler, "claim_hint_render", new=AsyncMock())

    await handler.handle_user_not_started_bot(bot, CHAT_ID, USER_ID)

    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_ID, message_id=4242)
    # 消息未纳入状态机，不得提交渲染版本，否则会挡住新窗口的首次编辑
    claim.assert_not_awaited()


async def test_promote_failure_deletes_orphan_message(mocker) -> None:
    """promote 抛错时消息已发出却无人回收，必须补删，避免群里永久残留引导消息。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value="owner-token"))
    mocker.patch.object(handler, "add_hint_user", new=AsyncMock(return_value=(True, False, 1)))
    mocker.patch.object(handler, "snapshot_hint_users", new=AsyncMock(return_value=([USER_ID], 1)))
    mocker.patch.object(
        handler, "promote_hint", new=AsyncMock(side_effect=RuntimeError("redis down"))
    )
    mocker.patch.object(handler, "delete_hint_reservation", new=AsyncMock(return_value=True))

    await handler.handle_user_not_started_bot(bot, CHAT_ID, USER_ID)

    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_ID, message_id=4242)


async def test_redis_failure_still_sends_plain_hint(mocker) -> None:
    """mention 状态机故障时降级为无 mention 的引导消息，不影响验证主流程。"""
    _patch_common(mocker)
    bot = _bot()
    mocker.patch.object(handler, "reserve_hint", new=AsyncMock(return_value="owner-token"))
    mocker.patch.object(
        handler, "add_hint_user", new=AsyncMock(side_effect=RuntimeError("redis down"))
    )
    mocker.patch.object(
        handler, "snapshot_hint_users", new=AsyncMock(side_effect=RuntimeError("redis down"))
    )
    mocker.patch.object(handler, "promote_hint", new=AsyncMock(return_value=True))
    mocker.patch.object(handler, "claim_hint_render", new=AsyncMock(return_value=False))

    await handler.handle_user_not_started_bot(bot, CHAT_ID, USER_ID)

    text = _sent_text(bot)
    assert text.startswith("⚠️ <b>入群验证提示</b>")
    assert "tg://user" not in text


@pytest.mark.parametrize("locale", ["zh-Hans", "zh-Hant", "en"])
def test_mentions_catalog_key_exists_in_all_locales(locale: str) -> None:
    """三语必须都有 mention 行文案且占位符一致（启动期 parity 校验的前置保障）。"""
    catalog = json.loads(Path(f"locales/{locale}.json").read_text(encoding="utf-8"))
    assert MENTIONS_KEY in catalog
    assert "{users}" in catalog[MENTIONS_KEY]
    # mention HTML 由代码生成，文案本身不得带标签，避免与插入值嵌套冲突
    assert "<" not in catalog[MENTIONS_KEY]
