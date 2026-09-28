"""自定义验证脚本 revision 数据访问层"""

from sqlalchemy import select, update

from src.core.database import get_db_session
from src.models.custom_verification import CustomVerificationRevision
from src.models.group import Group


class CustomVerificationRepository:
    """revision 的创建/查询/审查结果写入与激活切换。

    revision 不可变：无 update source / delete 接口（被在途验证会话引用的
    版本必须永久可查）；唯一可变字段是 review_result（三重审查结论）。
    """

    @staticmethod
    async def create_revision(revision: CustomVerificationRevision) -> CustomVerificationRevision:
        """写入一个新 revision（上传 = 新增，禁原地覆盖）。"""
        async with get_db_session() as session:
            session.add(revision)
            await session.commit()
            await session.refresh(revision)
            return revision

    @staticmethod
    async def get_revision(revision_id: int) -> CustomVerificationRevision | None:
        async with get_db_session() as session:
            result = await session.execute(
                select(CustomVerificationRevision).where(
                    CustomVerificationRevision.id == revision_id
                )
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def get_group_revision(
        group_id: int, revision_id: int
    ) -> CustomVerificationRevision | None:
        """取属于指定群的 revision（跨群引用一律拒绝）。"""
        async with get_db_session() as session:
            result = await session.execute(
                select(CustomVerificationRevision).where(
                    CustomVerificationRevision.id == revision_id,
                    CustomVerificationRevision.group_id == group_id,
                )
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def list_group_revisions(
        group_id: int, limit: int = 20
    ) -> list[CustomVerificationRevision]:
        async with get_db_session() as session:
            result = await session.execute(
                select(CustomVerificationRevision)
                .where(CustomVerificationRevision.group_id == group_id)
                .order_by(CustomVerificationRevision.id.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def update_review_result(revision_id: int, review_result: dict) -> bool:
        async with get_db_session() as session:
            result = await session.execute(
                update(CustomVerificationRevision)
                .where(CustomVerificationRevision.id == revision_id)
                .values(review_result=review_result)
            )
            await session.commit()
            # mypy: Result[Any] 实际上是 CursorResult，它有 rowcount 属性
            return bool(result.rowcount)  # type: ignore[attr-defined]

    @staticmethod
    async def activate_revision(
        group_id: int, revision_id: int, expected_current: int | None
    ) -> bool:
        """原子激活：CAS 切换 revision 指针并联动置位启用开关。

        乐观并发：expected_current 不符（他人已切换）则失败，防静默覆盖。
        注意 NULL 与整数的比较谓词不同：``IS`` 只能用于 NULL（PostgreSQL
        不支持整数 ``IS`` 比较），须按分支生成。
        """
        condition = (
            Group.active_revision_id.is_(None)
            if expected_current is None
            else Group.active_revision_id == expected_current
        )
        async with get_db_session() as session:
            result = await session.execute(
                update(Group)
                .where(Group.id == group_id, condition)
                .values(active_revision_id=revision_id, custom_verify_enabled=True)
            )
            await session.commit()
            # mypy: Result[Any] 实际上是 CursorResult，它有 rowcount 属性
            return bool(result.rowcount)  # type: ignore[attr-defined]

    @staticmethod
    async def set_custom_verify_enabled(group_id: int, enabled: bool) -> bool:
        """只切换启用开关（不动 revision 指针：停用后重新启用恢复同一版本）。"""
        async with get_db_session() as session:
            result = await session.execute(
                update(Group)
                .where(Group.id == group_id, Group.active_revision_id.isnot(None))
                .values(custom_verify_enabled=enabled)
            )
            await session.commit()
            # mypy: Result[Any] 实际上是 CursorResult，它有 rowcount 属性
            return bool(result.rowcount)  # type: ignore[attr-defined]

    @staticmethod
    async def get_active_revision(group_id: int) -> CustomVerificationRevision | None:
        """取群当前生效的 revision；未启用或引用缺失返回 None。"""
        async with get_db_session() as session:
            result = await session.execute(
                select(CustomVerificationRevision)
                .join(Group, Group.active_revision_id == CustomVerificationRevision.id)
                .where(Group.id == group_id, Group.active_revision_id.isnot(None))
            )
            return result.scalar_one_or_none()
