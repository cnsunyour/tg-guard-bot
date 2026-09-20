"""spam_samples 同文本去重与「人工标注优先」回归测试。

- 同一文本经 AI 自动入库 → bot 立即处罚 → 管理员反馈多次写入时只保留一行
- 人工标注覆盖任何既有标注并收敛历史重复行；自动标注不得覆盖任一人工标注
- 覆盖时刷新 created_at，使纠正后的样本参与下一轮训练
- add_feedback 透传结果：跳过（None）与失败均返回 False
"""

from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DBAPIError

from src.models.spam_sample import SpamSample
from src.repositories import spam_repo
from src.repositories.spam_repo import SpamRepository, is_automated_label
from src.services.spam_detector import SpamDetector

pytestmark = pytest.mark.unit

BOT_ID = 900001
AI_ID = -1
ADMIN_ID = 42
OTHER_ADMIN_ID = 43
TEXT = "这是一条用于验证样本去重优先级的中文消息"
OLD_TIME = datetime(2026, 1, 1)


@pytest.fixture(autouse=True)
def _fixed_identity(monkeypatch):
    """固定 bot / AI 身份，来源判定不受环境变量影响。"""
    monkeypatch.setattr(spam_repo.settings, "bot_token", f"{BOT_ID}:TEST")
    monkeypatch.setattr(spam_repo.settings, "ai_spam_labeled_by", AI_ID)


@pytest.fixture
def db(monkeypatch):
    """打桩 get_db_session：execute 返回可配置的同文本样本列表（最新在前）。"""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()

    @asynccontextmanager
    async def fake_session():
        yield session

    monkeypatch.setattr(spam_repo, "get_db_session", fake_session)
    return session


def _sample(sample_id: int, *, is_spam: bool, labeled_by: int | None) -> SpamSample:
    return SpamSample(
        id=sample_id,
        text=TEXT,
        is_spam=is_spam,
        confidence=0.9,
        labeled_by=labeled_by,
        created_at=OLD_TIME,
    )


def _existing(db, *samples: SpamSample) -> None:
    db.execute.return_value.scalars.return_value.all.return_value = list(samples)


# ===== 来源判定 =====
@pytest.mark.parametrize(
    "labeled_by,expected",
    [(AI_ID, True), (BOT_ID, True), (ADMIN_ID, False), (None, False), (0, False)],
)
def test_is_automated_label(labeled_by, expected):
    assert is_automated_label(labeled_by) is expected


# ===== 无同文本样本 → 新增 =====
@pytest.mark.parametrize("labeled_by", [AI_ID, BOT_ID, ADMIN_ID])
async def test_insert_when_absent(db, labeled_by):
    sample = await SpamRepository.upsert_sample(TEXT, True, 0.9, labeled_by)

    assert sample is not None
    assert (sample.text, sample.is_spam, sample.confidence, sample.labeled_by) == (
        TEXT,
        True,
        0.9,
        labeled_by,
    )
    db.add.assert_called_once_with(sample)
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(sample)


# ===== 人工标注覆盖一切 =====
@pytest.mark.parametrize("old_labeled_by", [AI_ID, BOT_ID, ADMIN_ID, OTHER_ADMIN_ID, None])
@pytest.mark.parametrize("old_is_spam", [True, False])
async def test_manual_label_overrides_existing(db, old_labeled_by, old_is_spam):
    sample = _sample(8, is_spam=old_is_spam, labeled_by=old_labeled_by)
    _existing(db, sample)

    saved = await SpamRepository.upsert_sample(TEXT, not old_is_spam, 1.0, ADMIN_ID)

    assert saved is sample
    assert (sample.is_spam, sample.confidence, sample.labeled_by) == (
        not old_is_spam,
        1.0,
        ADMIN_ID,
    )
    assert sample.id == 8
    assert sample.created_at > OLD_TIME  # 刷新时间，纳入下一轮训练取数
    db.add.assert_not_called()
    db.delete.assert_not_awaited()
    db.commit.assert_awaited_once()


# ===== 人工标注收敛历史重复行 =====
async def test_manual_label_collapses_duplicate_rows(db):
    """同文本存在「旧正样本 + 新负样本」时，/notspam 纠正后旧正样本一并删除。"""
    newest = _sample(9, is_spam=False, labeled_by=AI_ID)
    older_positive = _sample(8, is_spam=True, labeled_by=BOT_ID)
    oldest_positive = _sample(7, is_spam=True, labeled_by=ADMIN_ID)
    _existing(db, newest, older_positive, oldest_positive)

    saved = await SpamRepository.upsert_sample(TEXT, False, 1.0, OTHER_ADMIN_ID)

    assert saved is newest
    assert (newest.is_spam, newest.labeled_by) == (False, OTHER_ADMIN_ID)
    assert {call.args[0] for call in db.delete.await_args_list} == {
        older_positive,
        oldest_positive,
    }
    db.commit.assert_awaited_once()


# ===== 自动标注不得覆盖任一人工标注 =====
@pytest.mark.parametrize("new_labeled_by", [AI_ID, BOT_ID])
@pytest.mark.parametrize("manual_labeled_by", [ADMIN_ID, None, 0])
@pytest.mark.parametrize("manual_is_newest", [True, False], ids=["manual_newest", "manual_older"])
async def test_automated_label_preserves_manual(
    db, new_labeled_by, manual_labeled_by, manual_is_newest
):
    manual = _sample(8, is_spam=False, labeled_by=manual_labeled_by)
    automated = _sample(9, is_spam=True, labeled_by=AI_ID)
    _existing(db, *((manual, automated) if manual_is_newest else (automated, manual)))

    saved = await SpamRepository.upsert_sample(TEXT, True, 0.95, new_labeled_by)

    assert saved is None
    assert (manual.is_spam, manual.labeled_by, manual.created_at) == (
        False,
        manual_labeled_by,
        OLD_TIME,
    )
    assert (automated.is_spam, automated.created_at) == (True, OLD_TIME)
    db.add.assert_not_called()
    db.delete.assert_not_awaited()


# ===== 自动覆盖自动（含收敛） =====
@pytest.mark.parametrize("old_labeled_by", [AI_ID, BOT_ID])
@pytest.mark.parametrize("new_labeled_by", [AI_ID, BOT_ID])
async def test_automated_label_overrides_automated(db, old_labeled_by, new_labeled_by):
    newest = _sample(9, is_spam=True, labeled_by=old_labeled_by)
    duplicate = _sample(8, is_spam=True, labeled_by=old_labeled_by)
    _existing(db, newest, duplicate)

    saved = await SpamRepository.upsert_sample(TEXT, True, 0.8, new_labeled_by)

    assert saved is newest
    assert (newest.confidence, newest.labeled_by) == (0.8, new_labeled_by)
    db.delete.assert_awaited_once_with(duplicate)
    db.add.assert_not_called()


# ===== 端到端序列：AI → bot 处罚 → 管理员误判 → AI 再判垃圾 =====
async def test_ai_then_punishment_then_admin_keeps_single_row(db):
    sample = await SpamRepository.upsert_sample(TEXT, True, 0.9, AI_ID)
    assert sample is not None
    _existing(db, sample)

    assert await SpamRepository.upsert_sample(TEXT, True, 0.9, BOT_ID) is sample
    assert await SpamRepository.upsert_sample(TEXT, False, 1.0, ADMIN_ID) is sample
    # 管理员已判正常，后续 AI 再判垃圾不得回写
    assert await SpamRepository.upsert_sample(TEXT, True, 0.95, AI_ID) is None

    assert (sample.is_spam, sample.labeled_by) == (False, ADMIN_ID)
    db.add.assert_called_once_with(sample)  # 全程只新增一行
    db.delete.assert_not_awaited()
    assert db.commit.await_count == 3


# ===== add_feedback 透传 =====
@pytest.mark.parametrize("outcome", ["saved", "skipped", "failed"])
async def test_add_feedback_reports_upsert_outcome(monkeypatch, outcome):
    upsert = AsyncMock(return_value=object() if outcome == "saved" else None)
    if outcome == "failed":
        upsert.side_effect = RuntimeError("入库失败")
    monkeypatch.setattr(SpamRepository, "upsert_sample", upsert)
    detector = object.__new__(SpamDetector)

    success = await detector.add_feedback(text=TEXT, is_spam=True, labeled_by=AI_ID, confidence=0.9)

    assert success is (outcome == "saved")
    upsert.assert_awaited_once_with(text=TEXT, is_spam=True, confidence=0.9, labeled_by=AI_ID)


async def test_add_feedback_skips_blank_text(monkeypatch):
    upsert = AsyncMock()
    monkeypatch.setattr(SpamRepository, "upsert_sample", upsert)
    detector = object.__new__(SpamDetector)

    assert await detector.add_feedback(text="   ", is_spam=True, labeled_by=ADMIN_ID) is False
    upsert.assert_not_awaited()


# ===== 死锁重试 =====
def _deadlock_error() -> DBAPIError:
    orig = MagicMock()
    orig.sqlstate = "40P01"
    return DBAPIError("stmt", {}, orig)


async def test_upsert_retries_on_deadlock_then_succeeds(monkeypatch):
    """与负样本裁剪交错触发死锁时整事务重试，第二次成功。"""
    monkeypatch.setattr(spam_repo, "_UPSERT_RETRY_BACKOFF_SECONDS", 0)
    expected = object()
    once = AsyncMock(side_effect=[_deadlock_error(), expected])
    monkeypatch.setattr(SpamRepository, "_upsert_sample_once", once)

    assert await SpamRepository.upsert_sample(TEXT, True, 0.9, ADMIN_ID) is expected
    assert once.await_count == 2


async def test_upsert_gives_up_after_max_deadlock_attempts(monkeypatch):
    monkeypatch.setattr(spam_repo, "_UPSERT_RETRY_BACKOFF_SECONDS", 0)
    once = AsyncMock(side_effect=_deadlock_error())
    monkeypatch.setattr(SpamRepository, "_upsert_sample_once", once)

    with pytest.raises(DBAPIError):
        await SpamRepository.upsert_sample(TEXT, True, 0.9, ADMIN_ID)
    assert once.await_count == spam_repo._UPSERT_MAX_ATTEMPTS


async def test_upsert_does_not_retry_other_db_errors(monkeypatch):
    orig = MagicMock()
    orig.sqlstate = "23505"  # 唯一约束冲突等其它错误不重试
    once = AsyncMock(side_effect=DBAPIError("stmt", {}, orig))
    monkeypatch.setattr(SpamRepository, "_upsert_sample_once", once)

    with pytest.raises(DBAPIError):
        await SpamRepository.upsert_sample(TEXT, True, 0.9, ADMIN_ID)
    once.assert_awaited_once()
