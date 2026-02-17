"""垃圾样本数据仓库"""

import asyncio

from loguru import logger
from sqlalchemy import func, select

from src.core.database import get_db_session
from src.models.spam_sample import SpamSample


class SpamRepository:
    """垃圾样本数据仓库"""

    @staticmethod
    async def add_sample(
        text: str,
        is_spam: bool,
        confidence: float | None = None,
        labeled_by: int | None = None,
    ) -> SpamSample:
        """添加样本

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

            query = query.order_by(SpamSample.created_at.desc())

            if limit is not None:
                query = query.limit(limit)

            result = await session.execute(query)
            return list(result.scalars().all())

    @staticmethod
    async def get_training_data() -> tuple[list[str], list[bool]]:
        """获取训练数据（平衡样本策略）

        策略：
        1. 获取全部正样本
        2. 获取最新的负样本，数量为正样本的 10 倍
        3. 如果负样本总数不足，则使用全部可用的负样本

        Returns:
            (文本列表, 标签列表)
        """
        # 1. 统计正负样本数量
        spam_count, normal_count = await SpamRepository.count_samples_by_label()

        if spam_count == 0:
            logger.warning("没有正样本，返回空训练集")
            return [], []

        # 2. 计算需要的负样本数量（正样本的 20 倍）
        target_normal_count = spam_count * 20
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

            query = query.order_by(SpamSample.created_at.desc()).limit(1)

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
        from sqlalchemy import and_, delete

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


# 全局变量：记录上次训练时的样本数量
_last_train_sample_count = 0


def get_last_train_count() -> int:
    """获取上次训练时的样本数量"""
    return _last_train_sample_count


def update_last_train_count(count: int) -> None:
    """更新上次训练时的样本数量"""
    global _last_train_sample_count
    _last_train_sample_count = count
