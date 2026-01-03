"""垃圾样本数据仓库"""

from typing import List, Optional, Tuple
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.spam_sample import SpamSample
from src.core.database import get_db_session


class SpamRepository:
    """垃圾样本数据仓库"""

    @staticmethod
    async def add_sample(
        text: str,
        is_spam: bool,
        confidence: Optional[float] = None,
        labeled_by: Optional[int] = None,
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
        is_spam: Optional[bool] = None, limit: Optional[int] = None
    ) -> List[SpamSample]:
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
    async def get_training_data() -> Tuple[List[str], List[bool]]:
        """获取训练数据

        Returns:
            (文本列表, 标签列表)
        """
        samples = await SpamRepository.get_all_samples()

        texts = [sample.text for sample in samples]
        labels = [sample.is_spam for sample in samples]

        return texts, labels

    @staticmethod
    async def count_samples(is_spam: Optional[bool] = None) -> int:
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
    async def delete_sample(sample_id: int) -> bool:
        """删除样本

        Args:
            sample_id: 样本 ID
        """
        async with get_db_session() as session:
            result = await session.execute(
                select(SpamSample).where(SpamSample.id == sample_id)
            )
            sample = result.scalar_one_or_none()

            if sample:
                await session.delete(sample)
                await session.commit()
                return True

            return False

    @staticmethod
    async def get_recent_samples(limit: int = 100) -> List[SpamSample]:
        """获取最近的样本

        Args:
            limit: 限制数量
        """
        async with get_db_session() as session:
            result = await session.execute(
                select(SpamSample)
                .order_by(SpamSample.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def update_sample_label(
        sample_id: int, is_spam: bool, labeled_by: int
    ) -> bool:
        """更新样本标签

        Args:
            sample_id: 样本 ID
            is_spam: 新的标签
            labeled_by: 标注者 ID
        """
        async with get_db_session() as session:
            result = await session.execute(
                select(SpamSample).where(SpamSample.id == sample_id)
            )
            sample = result.scalar_one_or_none()

            if sample:
                sample.is_spam = is_spam
                sample.labeled_by = labeled_by
                await session.commit()
                return True

            return False
