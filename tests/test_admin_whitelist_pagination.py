"""/whitelist list 分页测试（C 段 #9）。

覆盖：
- 空列表 → 单条 empty 消息
- 少量群组（不超字符预算）→ 单条消息（header + rows）
- 大量群组（超字符预算）→ 多条消息，第 2+ 条用 continuation 标识 + 递增页码
- catalog continuation.message 三语存在 + 占位符对等
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.handlers.admin import _WHITELIST_PAGE_CHAR_LIMIT, _list_whitelist

pytestmark = pytest.mark.unit


def _localizer(row_len: int = 10) -> MagicMock:
    """mock localizer：返回可控长度的 header/row/continuation，便于精确触发分页。

    row_len 控制每行渲染长度（含固定前缀 + 填充）。
    """
    loc = MagicMock()

    def fake_t(key: str, **kw):
        if "header" in key:
            return "H\n\n"  # 3 字符
        if "row" in key:
            idx = kw.get("index", 0)
            # 固定前缀 + 填充到 row_len
            base = f"[{idx}] "
            return base + "x" * max(0, row_len - len(base)) + "\n"
        if "continuation" in key:
            return f"CONT(p{kw.get('page')})\n\n"
        if "empty" in key:
            return "EMPTY"
        if "unknown_group" in key:
            return "?"
        if "failed" in key:
            return "FAIL"
        return key

    loc.t.side_effect = fake_t
    return loc


def _make_groups(n: int, title: str | None = "群") -> list[MagicMock]:
    """构造 n 个 mock Group（id + title）。"""
    groups: list[MagicMock] = []
    for i in range(n):
        g = MagicMock()
        g.id = -1000000 + i
        g.title = title
        groups.append(g)
    return groups


def _message_with_answer_tracker() -> MagicMock:
    """构造 mock Message，answer 记录所有调用文本。"""
    message = MagicMock()
    message.answer = AsyncMock()
    message.chat = MagicMock()
    message.chat.id = -100
    return message


# ===== 空列表 =====
async def test_empty_list_sends_single_empty_message() -> None:
    """空列表 → 1 条 empty.message。"""
    message = _message_with_answer_tracker()
    with patch(
        "src.bot.handlers.admin.GroupRepository.get_whitelisted_groups",
        new=AsyncMock(return_value=[]),
    ):
        await _list_whitelist(message, _localizer())
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "EMPTY"


# ===== 单页（不超预算）=====
async def test_few_groups_fit_single_page() -> None:
    """3 群组 → 1 条消息（header + 3 row）。"""
    message = _message_with_answer_tracker()
    groups = _make_groups(3)
    with patch(
        "src.bot.handlers.admin.GroupRepository.get_whitelisted_groups",
        new=AsyncMock(return_value=groups),
    ):
        await _list_whitelist(message, _localizer(row_len=10))
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text.startswith("H\n\n")
    assert "[1]" in text and "[2]" in text and "[3]" in text


# ===== 多页（超预算）=====
async def test_many_groups_split_into_pages_with_continuation() -> None:
    """群组超字符预算 → 多条消息，第 2+ 条含 continuation + 递增页码。"""
    message = _message_with_answer_tracker()
    # 每行 ~12 字符，limit 压到 30 → 第 3 行左右触发分页
    groups = _make_groups(8)
    with (
        patch(
            "src.bot.handlers.admin.GroupRepository.get_whitelisted_groups",
            new=AsyncMock(return_value=groups),
        ),
        patch("src.bot.handlers.admin._WHITELIST_PAGE_CHAR_LIMIT", 30),
    ):
        await _list_whitelist(message, _localizer(row_len=12))

    assert message.answer.await_count >= 2
    texts = [call.args[0] for call in message.answer.await_args_list]
    # 第 1 页用 header，不含 continuation
    assert texts[0].startswith("H\n\n")
    assert "CONT" not in texts[0]
    # 第 2+ 页用 continuation + 页码（2, 3, ...）
    for i, text in enumerate(texts[1:], start=2):
        assert f"CONT(p{i})" in text
    # 所有 8 个 row 都被发送（跨页不丢）
    all_text = "".join(texts)
    for idx in range(1, 9):
        assert f"[{idx}]" in all_text


# ===== catalog continuation.message 三语 parity =====
def test_continuation_key_exists_in_three_locales() -> None:
    """admin.whitelist.list.continuation.message 三语存在 + 占位符 {page} 对等。"""
    root = Path(__file__).resolve().parents[1]
    import re

    for locale in ("zh-Hans", "zh-Hant", "en"):
        catalog = json.loads((root / "locales" / f"{locale}.json").read_text("utf-8"))
        val = catalog.get("admin.whitelist.list.continuation.message")
        assert val is not None, f"{locale} 缺 continuation.message"
        placeholders = re.findall(r"\{(\w+)\}", val)
        assert placeholders == ["page"], f"{locale} 占位符应为 [page]，实为 {placeholders}"


# ===== 常量值合理（给 4096 留余量）=====
def test_page_char_limit_leaves_margin_under_telegram_cap() -> None:
    """3500 预算给 Telegram 4096 上限留 ~600 余量（HTML 实体膨胀 + 多语言）。"""
    assert _WHITELIST_PAGE_CHAR_LIMIT <= 3500
    assert _WHITELIST_PAGE_CHAR_LIMIT < 4096
