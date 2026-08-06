"""用户设置数据仓库"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.core.database import get_db_session
from src.models.user_settings import UserSettings


class UserSettingsRepository:
    """用户私聊设置仓库"""

    @staticmethod
    async def get_locale(user_id: int) -> str | None:
        """获取用户显式保存的语言偏好

        没有记录时返回 None，由 LocaleResolver 决定后续 fallback。
        """
        async with get_db_session() as session:
            result = await session.execute(
                select(UserSettings.locale).where(UserSettings.user_id == user_id)
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def upsert_locale(user_id: int, locale: str) -> None:
        """新增或更新用户语言偏好（PostgreSQL ON CONFLICT upsert）"""
        now = datetime.utcnow()
        async with get_db_session() as session:
            statement = (
                insert(UserSettings)
                .values(user_id=user_id, locale=locale, updated_at=now)
                .on_conflict_do_update(
                    index_elements=[UserSettings.user_id],
                    set_={"locale": locale, "updated_at": now},
                )
            )
            await session.execute(statement)
            await session.commit()

    @staticmethod
    async def get_users_with_custom_locale(default_locale: str) -> list[tuple[int, str]]:
        """获取所有 locale 与默认值不同的用户（user_id, locale）。

        用于启动时恢复命令菜单（3c3）：非默认 locale 的用户需 sync 对应语言命令。
        """
        async with get_db_session() as session:
            result = await session.execute(
                select(UserSettings.user_id, UserSettings.locale)
                .where(UserSettings.locale != default_locale)
                .order_by(UserSettings.user_id)
            )
            return [(row[0], row[1]) for row in result.all()]
