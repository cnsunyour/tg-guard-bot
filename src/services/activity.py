"""用户活跃度服务模块"""

import math
from datetime import date

from loguru import logger

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis


class ActivityService:
    """用户活跃度服务

    活跃度规则:
    - 初始值: 0
    - 文本消息: +1
    - 非文本消息: -2 (活跃度 > 0 时) 或 阻止发送 (活跃度 <= 0)
    - 每日衰减: -1 (无消息时，懒惰计算)
    """

    # 非文本消息扣分值
    NON_TEXT_PENALTY = 2

    # 文本消息加分值
    TEXT_REWARD = 1

    @staticmethod
    async def get_activity(chat_id: int, user_id: int) -> int:
        """获取用户活跃度 (含衰减计算)

        使用懒惰衰减策略：读取时根据最后消息日期计算衰减

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            活跃度值 (可能为负数)
        """
        redis = get_redis()
        activity_key = RedisKeys.user_activity(chat_id, user_id)
        last_date_key = RedisKeys.activity_last_date(chat_id, user_id)

        # 获取存储的活跃度
        stored_activity_str = await redis.get(activity_key)
        if stored_activity_str is None:
            return 0  # 新用户默认 0

        stored_activity = int(stored_activity_str)

        # 获取最后消息日期
        last_date_str = await redis.get(last_date_key)
        if last_date_str is None:
            return stored_activity  # 无日期记录，不衰减

        # 计算衰减
        try:
            last_date_obj = date.fromisoformat(last_date_str)
            days_passed = (date.today() - last_date_obj).days

            if days_passed > 0:
                actual_activity = stored_activity - days_passed

                # 可选：清理严重负数数据（释放 Redis 内存）
                if actual_activity <= -30:
                    await redis.delete(activity_key, last_date_key)
                    return 0

                return actual_activity
        except ValueError:
            logger.warning(f"活跃度日期解析失败 [群组:{chat_id}] [用户:{user_id}]")
            pass

        return stored_activity

    @staticmethod
    async def record_text_message(chat_id: int, user_id: int) -> int:
        """记录文本消息，增加活跃度

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            更新后的活跃度
        """
        redis = get_redis()
        activity_key = RedisKeys.user_activity(chat_id, user_id)
        last_date_key = RedisKeys.activity_last_date(chat_id, user_id)

        # 先获取当前实际活跃度 (含衰减)
        current = await ActivityService.get_activity(chat_id, user_id)

        # 增加活跃度
        new_activity = current + ActivityService.TEXT_REWARD

        # 更新 Redis
        await redis.set(activity_key, str(new_activity))
        await redis.set(last_date_key, date.today().isoformat())

        logger.debug(
            f"活跃度增加 [群组:{chat_id}] [用户:{user_id}] {current} -> {new_activity}"
        )

        return new_activity

    @staticmethod
    async def check_non_text_allowed(chat_id: int, user_id: int) -> tuple[bool, int]:
        """检查用户是否可以发送非文本消息

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            (是否允许, 当前活跃度)
        """
        current = await ActivityService.get_activity(chat_id, user_id)

        if current <= 0:
            return False, current

        return True, current

    @staticmethod
    async def record_non_text_message(chat_id: int, user_id: int) -> int:
        """记录非文本消息，扣除活跃度 (仅在活跃度 > 0 时调用)

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            更新后的活跃度
        """
        redis = get_redis()
        activity_key = RedisKeys.user_activity(chat_id, user_id)
        last_date_key = RedisKeys.activity_last_date(chat_id, user_id)

        # 先获取当前实际活跃度 (含衰减)
        current = await ActivityService.get_activity(chat_id, user_id)

        # 扣除活跃度
        new_activity = current - ActivityService.NON_TEXT_PENALTY

        # 更新 Redis
        await redis.set(activity_key, str(new_activity))
        await redis.set(last_date_key, date.today().isoformat())

        logger.debug(
            f"活跃度减少 [群组:{chat_id}] [用户:{user_id}] {current} -> {new_activity}"
        )

        return new_activity

    @staticmethod
    def calculate_confidence_reduction(activity: int) -> float:
        """根据活跃度计算反垃圾置信度减少值

        使用对数公式，实现边际递减效应

        公式: confidence_reduction = 0.01 * log2(activity / 10)
        - activity = 10: 0.01
        - activity = 20: 0.02
        - activity = 40: 0.03
        - activity = 80: 0.04
        - activity = 160: 0.05
        - 最大减少: activity_max_confidence_reduction (默认 0.15)

        Args:
            activity: 用户活跃度

        Returns:
            置信度减少值 (负数，范围 0 到 -activity_max_confidence_reduction)
        """
        if activity < 10:
            return 0.0

        # 特殊处理 activity = 10 的情况
        if activity == 10:
            return 0.01

        # log2(activity / 10)
        reduction = 0.01 * math.log2(activity / 10)

        # 限制最大减少值
        max_reduction = settings.activity_max_confidence_reduction
        return min(reduction, max_reduction)
