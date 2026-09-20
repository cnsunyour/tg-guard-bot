"""垃圾样本数据仓库"""

import asyncio

from aiogram.utils.token import extract_bot_id
from loguru import logger
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import DBAPIError

from src.core.config import settings
from src.core.database import DEFAULT_DELETE_BATCH_SIZE, delete_in_batches, get_db_session
from src.core.utils import utcnow_naive
from src.models.spam_sample import SpamSample

# 训练取数与定时清理共用的负样本比例：正样本 1 份对应取最新 20 份负样本。
# get_training_data() 与 DataCleanupService（数据裁剪）必须引用同一常量，防止两处口径漂移。
# ⚠️ 修改此值会同时改变训练取数与清理保留口径——定时清理将按新比例**永久删除**
# 不再参与训练的负样本（不可恢复），调整前须评估数据删除影响。
NEGATIVE_SAMPLES_PER_POSITIVE = 20

# upsert_sample 遇到 PostgreSQL 死锁（与负样本裁剪交错）时的整事务重试次数与退避
_UPSERT_MAX_ATTEMPTS = 3
_UPSERT_RETRY_BACKOFF_SECONDS = 0.05
_PG_DEADLOCK_SQLSTATE = "40P01"


def _is_deadlock(error: DBAPIError) -> bool:
    """判断驱动异常是否为 PostgreSQL 死锁（SQLSTATE 40P01），不耦合具体驱动类。"""
    return getattr(error.orig, "sqlstate", None) == _PG_DEADLOCK_SQLSTATE


def _automated_labeler_ids() -> frozenset[int]:
    """自动标注者 ID 集合：AI 保留 ID 与 bot 自身 ID（立即处罚入库）。

    从配置推导而非运行时记忆，保证进程刚启动、尚未发生任何处罚时也能识别
    历史行的来源。除此之外的 labeled_by（含 None）一律视为人工标注。
    """
    return frozenset({settings.ai_spam_labeled_by, extract_bot_id(settings.bot_token)})


def is_automated_label(labeled_by: int | None) -> bool:
    """判断标注者是否为自动来源（AI / bot），用于人工优先的覆盖规则。"""
    return labeled_by is not None and labeled_by in _automated_labeler_ids()


class SpamRepository:
    """垃圾样本数据仓库"""

    @staticmethod
    async def add_sample(
        text: str,
        is_spam: bool,
        confidence: float | None = None,
        labeled_by: int | None = None,
    ) -> SpamSample:
        """添加样本（无条件新增一行；需要去重语义时用 upsert_sample）

        Args:
            text: 消息文本
            is_spam: 是否为垃圾
            confidence: 置信度
            labeled_by: 标注者 ID
        """
        async with get_db_session() as session:
            sample = SpamSample(
                text=text,
                is_spam=is_spam,
                confidence=confidence,
                labeled_by=labeled_by,
            )
            session.add(sample)
            await session.commit()
            await session.refresh(sample)
            return sample

    @staticmethod
    async def upsert_sample(
        text: str,
        is_spam: bool,
        confidence: float | None = None,
        labeled_by: int | None = None,
    ) -> SpamSample | None:
        """按精确文本写入样本：同文本只保留一行，人工标注优先。

        同一条消息会经过多个入库入口（AI 自动标注 → bot 立即处罚 → 管理员反馈 /
        /spam / 举报批准 / 投票终局），原先各写一行导致训练时重复计权。规则：
        - 无同文本样本 → 新增
        - 本次为人工标注 → 覆盖最新行、删除其余同文本行（管理员决定始终生效）
        - 本次为自动标注、同文本任一行为人工标注 → 跳过，返回 None
        - 本次为自动标注、同文本全为自动标注 → 覆盖最新行、删除其余行

        覆盖时刷新 created_at：训练取数与负样本裁剪都按该字段取最新，纠正后的
        样本应参与下一轮训练而不是因沿用旧时间被立刻裁掉。

        历史遗留的同文本重复行在下一次写入时顺带收敛为一行。已知限制：无唯一
        约束，并发首次写入仍可能各插一行（下次写入时收敛）。

        本方法锁多行的顺序（新→旧）与负样本裁剪的批量 DELETE 加锁顺序无法对齐，
        两者交错时 PostgreSQL 可能判定死锁并中止其中一方；对死锁整体重试事务。

        Returns:
            写入后的样本；因人工标注保护而跳过时返回 None
        """
        for attempt in range(1, _UPSERT_MAX_ATTEMPTS + 1):
            try:
                return await SpamRepository._upsert_sample_once(
                    text, is_spam, confidence, labeled_by
                )
            except DBAPIError as e:
                if not _is_deadlock(e) or attempt == _UPSERT_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    f"样本写入遇到数据库死锁，重试 [第 {attempt}/{_UPSERT_MAX_ATTEMPTS} 次] "
                    f"[标注者:{labeled_by}]"
                )
                await asyncio.sleep(_UPSERT_RETRY_BACKOFF_SECONDS * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    async def _upsert_sample_once(
        text: str,
        is_spam: bool,
        confidence: float | None,
        labeled_by: int | None,
    ) -> SpamSample | None:
        """upsert_sample 的单次事务实现（规则见 upsert_sample）。"""
        automated = is_automated_label(labeled_by)

        async with get_db_session() as session:
            # 锁住全部同文本行：人工保护须看所有行，并发标注不得互相覆盖
            result = await session.execute(
                select(SpamSample)
                .where(SpamSample.text == text)
                .order_by(SpamSample.created_at.desc(), SpamSample.id.desc())
                .with_for_update()
            )
            existing = list(result.scalars().all())

            if not existing:
                sample = SpamSample(
                    text=text,
                    is_spam=is_spam,
                    confidence=confidence,
                    labeled_by=labeled_by,
                )
                session.add(sample)
                await session.commit()
                await session.refresh(sample)
                return sample

            if automated:
                manual = next((s for s in existing if not is_automated_label(s.labeled_by)), None)
                if manual is not None:
                    logger.debug(
                        f"同文本已有人工标注，跳过自动样本写入 "
                        f"[样本ID:{manual.id}] [人工标注者:{manual.labeled_by}] "
                        f"[自动标注者:{labeled_by}]"
                    )
                    return None

            sample, *duplicates = existing
            logger.debug(
                f"覆盖同文本样本 [样本ID:{sample.id}] "
                f"[{sample.is_spam}→{is_spam}] [标注者:{sample.labeled_by}→{labeled_by}]"
                + (f" [收敛重复行:{len(duplicates)}]" if duplicates else "")
            )
            sample.is_spam = is_spam
            sample.confidence = confidence
            sample.labeled_by = labeled_by
            sample.created_at = utcnow_naive()
            for duplicate in duplicates:
                await session.delete(duplicate)

            await session.commit()
            await session.refresh(sample)
            return sample

    @staticmethod
    async def get_all_samples(
        is_spam: bool | None = None, limit: int | None = None
    ) -> list[SpamSample]:
        """获取所有样本

        Args:
            is_spam: 过滤条件，None 表示获取所有样本
            limit: 限制数量
        """
        async with get_db_session() as session:
            query = select(SpamSample)

            if is_spam is not None:
                query = query.where(SpamSample.is_spam == is_spam)

            # id 作并列序：批量入库时 created_at 常相同，必须与
            # prune_negative_samples 的保留排序严格同序，保证「训练取的 = 清理留的」
            query = query.order_by(SpamSample.created_at.desc(), SpamSample.id.desc())

            if limit is not None:
                query = query.limit(limit)

            result = await session.execute(query)
            return list(result.scalars().all())

    @staticmethod
    async def get_training_data() -> tuple[list[str], list[bool]]:
        """获取训练数据（平衡样本策略）

        策略：
        1. 获取全部正样本
        2. 获取最新的负样本，数量为正样本的 NEGATIVE_SAMPLES_PER_POSITIVE 倍（当前 20 倍）
        3. 如果负样本总数不足，则使用全部可用的负样本

        Returns:
            (文本列表, 标签列表)
        """
        # 1. 统计正负样本数量
        spam_count, normal_count = await SpamRepository.count_samples_by_label()

        if spam_count == 0:
            logger.warning("没有正样本，返回空训练集")
            return [], []

        # 2. 计算需要的负样本数量（正样本的 NEGATIVE_SAMPLES_PER_POSITIVE 倍）
        target_normal_count = spam_count * NEGATIVE_SAMPLES_PER_POSITIVE
        actual_normal_count = min(target_normal_count, normal_count)

        logger.info(
            f"样本提取策略: "
            f"正样本={spam_count} (全部), "
            f"负样本={actual_normal_count}/{normal_count} (最新{actual_normal_count}个)"
        )

        # 3. 并行获取正负样本（提高性能）
        spam_samples_task = SpamRepository.get_all_samples(is_spam=True, limit=None)
        normal_samples_task = SpamRepository.get_all_samples(
            is_spam=False, limit=actual_normal_count
        )

        spam_samples, normal_samples = await asyncio.gather(spam_samples_task, normal_samples_task)

        # 4. 合并样本
        samples = spam_samples + normal_samples

        texts = [sample.text for sample in samples]
        labels = [sample.is_spam for sample in samples]

        return texts, labels

    @staticmethod
    async def count_samples(is_spam: bool | None = None) -> int:
        """统计样本数量

        Args:
            is_spam: 过滤条件，None 表示统计所有样本
        """
        async with get_db_session() as session:
            query = select(func.count(SpamSample.id))

            if is_spam is not None:
                query = query.where(SpamSample.is_spam == is_spam)

            result = await session.execute(query)
            return result.scalar() or 0

    @staticmethod
    async def count_samples_by_label() -> tuple[int, int]:
        """统计正负样本数量

        Returns:
            (正样本数量, 负样本数量)
        """
        async with get_db_session() as session:
            # 统计正样本
            spam_result = await session.execute(
                select(func.count(SpamSample.id)).where(SpamSample.is_spam.is_(True))
            )
            spam_count = spam_result.scalar() or 0

            # 统计负样本
            normal_result = await session.execute(
                select(func.count(SpamSample.id)).where(SpamSample.is_spam.is_(False))
            )
            normal_count = normal_result.scalar() or 0

            return spam_count, normal_count

    @staticmethod
    async def delete_sample(sample_id: int) -> bool:
        """删除样本

        Args:
            sample_id: 样本 ID
        """
        async with get_db_session() as session:
            result = await session.execute(select(SpamSample).where(SpamSample.id == sample_id))
            sample = result.scalar_one_or_none()

            if sample:
                await session.delete(sample)
                await session.commit()
                return True

            return False

    @staticmethod
    async def get_recent_samples(limit: int = 100) -> list[SpamSample]:
        """获取最近的样本

        Args:
            limit: 限制数量
        """
        async with get_db_session() as session:
            result = await session.execute(
                select(SpamSample).order_by(SpamSample.created_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def update_sample_label(sample_id: int, is_spam: bool, labeled_by: int) -> bool:
        """更新样本标签

        Args:
            sample_id: 样本 ID
            is_spam: 新的标签
            labeled_by: 标注者 ID
        """
        async with get_db_session() as session:
            result = await session.execute(select(SpamSample).where(SpamSample.id == sample_id))
            sample = result.scalar_one_or_none()

            if sample:
                sample.is_spam = is_spam
                sample.labeled_by = labeled_by
                await session.commit()
                return True

            return False

    @staticmethod
    async def find_sample_by_text(text: str, is_spam: bool | None = None) -> SpamSample | None:
        """通过文本查找样本

        Args:
            text: 消息文本
            is_spam: 是否为垃圾（可选过滤条件）

        Returns:
            找到的样本，未找到返回 None
        """
        async with get_db_session() as session:
            query = select(SpamSample).where(SpamSample.text == text)

            if is_spam is not None:
                query = query.where(SpamSample.is_spam == is_spam)

            query = query.order_by(SpamSample.created_at.desc(), SpamSample.id.desc()).limit(1)

            result = await session.execute(query)
            return result.scalar_one_or_none()

    @staticmethod
    async def delete_sample_by_text(text: str, is_spam: bool) -> int:
        """删除指定文本和标签的样本

        Args:
            text: 消息文本
            is_spam: 是否为垃圾

        Returns:
            删除的样本数量
        """
        async with get_db_session() as session:
            result = await session.execute(
                delete(SpamSample).where(
                    and_(
                        SpamSample.text == text,
                        SpamSample.is_spam == is_spam,
                    )
                )
            )
            await session.commit()
            # mypy: Result[Any] 实际上是 CursorResult，它有 rowcount 属性
            return int(result.rowcount)  # type: ignore[attr-defined]

    @staticmethod
    async def prune_negative_samples(
        keep_count: int, batch_size: int = DEFAULT_DELETE_BATCH_SIZE
    ) -> int:
        """裁剪多余负样本，仅保留最新的 keep_count 条

        正样本永久保留，本方法只处理 is_spam=False 的行。保留口径与
        get_training_data() 一致（(created_at, id) 最新优先），保证被删除的
        样本恰好是不会参与训练的部分。

        实现上先一次性查出保留边界（第 keep_count 新的 (created_at, id)），
        之后每批按固定范围从旧到新删除：避免每批重算 top-K 保留集，且窗口内
        并发新插入的负样本（created_at 更新）天然落在保留范围内不受影响。

        Args:
            keep_count: 需保留的负样本数量，必须为正（0 会删除全部负样本，
                调用方须自行守卫后传入正数）
            batch_size: 分批删除的单批数量，避免长事务

        Returns:
            删除的样本总数
        """
        if keep_count <= 0:
            raise ValueError(
                f"keep_count 必须为正数（当前 {keep_count}）：0/负数会删除全部负样本，"
                "调用方须确认保留口径后传入正数"
            )

        async with get_db_session() as session:
            # 1. 保留边界 = 第 keep_count 新的负样本行（(created_at, id) 序）；
            #    查不到说明负样本总数不足 keep_count，无需裁剪
            boundary_result = await session.execute(
                select(SpamSample.created_at, SpamSample.id)
                .where(SpamSample.is_spam.is_(False))
                .order_by(SpamSample.created_at.desc(), SpamSample.id.desc())
                .offset(keep_count - 1)
                .limit(1)
            )
            boundary = boundary_result.first()
            if boundary is None:
                return 0
            boundary_created_at, boundary_id = boundary

            # 2. 边界之外的负样本（(created_at, id) 序更旧者）即待删 victim
            older_than_boundary = or_(
                SpamSample.created_at < boundary_created_at,
                and_(
                    SpamSample.created_at == boundary_created_at,
                    SpamSample.id < boundary_id,
                ),
            )
            victim_select = (
                select(SpamSample.id)
                .where(SpamSample.is_spam.is_(False), older_than_boundary)
                .order_by(SpamSample.created_at.asc(), SpamSample.id.asc())
            )

            # 3. DELETE 再次校验 is_spam=False 与时间边界：victim 选取与删除之间若有
            #    并发把样本改标为正样本、或人工纠正刷新了 created_at（upsert_sample），
            #    跳过该行，守住「正样本永久保留」与「纠正后的样本参与下轮训练」
            return await delete_in_batches(
                session,
                victim_select,
                SpamSample,
                batch_size=batch_size,
                extra_conditions=(SpamSample.is_spam.is_(False), older_than_boundary),
            )


# 全局变量：记录上次训练时的样本数量
_last_train_sample_count = 0


def get_last_train_count() -> int:
    """获取上次训练时的样本数量"""
    return _last_train_sample_count


def update_last_train_count(count: int) -> None:
    """更新上次训练时的样本数量"""
    global _last_train_sample_count
    _last_train_sample_count = count
