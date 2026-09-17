"""用户活跃度服务模块"""

import math
from datetime import date

from loguru import logger

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis
from src.core.utils import calculate_normalized_length


class ActivityService:
    """用户活跃度服务

    活跃度规则:
    - 初始值: 0
    - 文本消息: +1（标准化长度 >= spam_min_text_length 才计；超短消息——
      单个标点、纯空白、极短互动等——不加分也不刷新衰减时钟，防占位消息
      刷活跃度解锁新人非文本限制）
    - 非文本消息: 0 (不扣分)
    - 每日衰减: -1 (仅当当前活跃度 > activity_decay_floor 且 < 10 时衰减；>= 10 不衰减)
      衰减结果最低保留 activity_decay_floor (默认1)；活跃度 <= floor (含 0) 不衰减保持原值

    活跃度用途:
    - 非文本消息限制: group.activity_enabled=True 时，活跃度 == 0（从未发言）才不能发非文本
    - 置信度修正: 活跃度越高，垃圾检测误判率越低（始终生效）
    - 检测豁免: activity >= threshold 时，跳过垃圾检测（始终生效）
    - 宵禁门槛: 宵禁期间根据活跃度控制发言权限（始终生效）
    """

    # 非文本消息扣分值
    NON_TEXT_PENALTY = 0

    # 文本消息加分值
    TEXT_REWARD = 1

    @staticmethod
    async def get_activity(chat_id: int, user_id: int, *, persist_decay: bool = True) -> int:
        """获取用户活跃度 (含衰减计算)

        使用懒惰衰减策略：读取时根据最后消息日期计算衰减

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            persist_decay: 是否将懒惰衰减结果写回 Redis。无意义消息记录路径传
                False，避免仅读取活跃度就重置衰减时钟

        Returns:
            活跃度值 (最小为 0)
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
            # ✅ 防御性检查：确保不返回历史负数数据
            return max(stored_activity, 0)

        # 计算衰减
        try:
            last_date_obj = date.fromisoformat(last_date_str)
            days_passed = (date.today() - last_date_obj).days

            if days_passed > 0:
                if stored_activity < 10:
                    # 活跃度低于 10 时才衰减，每天 -1
                    # 仅当当前活跃度高于 floor 时才衰减，避免 <= floor 的值反被抬升
                    if stored_activity > settings.activity_decay_floor:
                        actual_activity = max(
                            stored_activity - days_passed, settings.activity_decay_floor
                        )
                    else:
                        actual_activity = max(stored_activity, 0)
                else:
                    # 活跃度 >= 10 时不衰减
                    actual_activity = stored_activity

                if persist_decay:
                    # 更新 Redis 存储为正确的值（避免下次读取时重复衰减）
                    await redis.set(activity_key, str(actual_activity))
                    await redis.set(last_date_key, date.today().isoformat())

                return actual_activity
        except ValueError:
            logger.warning(f"活跃度日期解析失败 [群组:{chat_id}] [用户:{user_id}]")
            pass

        # ✅ 防御性检查：确保不返回历史负数数据
        return max(stored_activity, 0)

    @staticmethod
    async def record_text_message(chat_id: int, user_id: int, text: str | None = None) -> int:
        """记录文本消息，增加活跃度

        传入 ``text`` 且标准化长度低于 ``spam_min_text_length``（与垃圾检测的
        最小文本长度共用阈值）时视为超短消息：不加分也不刷新衰减时钟，防止
        垃圾号发一个「.」就把活跃度从 0 抬到 1、解锁新人非文本消息限制。
        ``text=None`` 保持原有行为（无条件 +1）。

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            text: 消息文本；低于最小文本长度的消息不增加活跃度

        Returns:
            更新后的活跃度（超短消息返回当前值）
        """
        if text is not None and calculate_normalized_length(text) < settings.spam_min_text_length:
            # persist_decay=False：仅读取活跃度也不写回，避免重置衰减时钟
            current = await ActivityService.get_activity(chat_id, user_id, persist_decay=False)
            logger.debug(
                f"忽略超短消息的活跃度奖励 " f"[群组:{chat_id}] [用户:{user_id}] [活跃度:{current}]"
            )
            return current

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

        logger.debug(f"活跃度增加 [群组:{chat_id}] [用户:{user_id}] {current} -> {new_activity}")

        return new_activity

    @staticmethod
    async def check_non_text_allowed(
        chat_id: int, user_id: int, check_enabled: bool
    ) -> tuple[bool, int]:
        """检查用户是否可以发送非文本消息

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            check_enabled: 是否启用限制检查（来自 group.activity_enabled）

        Returns:
            (是否允许, 当前活跃度)
        """
        current = await ActivityService.get_activity(chat_id, user_id)

        # 如果未启用限制，直接允许
        if not check_enabled:
            return True, current

        # 启用限制：活跃度 <= 0 时不允许
        if current <= 0:
            return False, current

        return True, current

    @staticmethod
    async def record_non_text_message(chat_id: int, user_id: int) -> int:
        """记录非文本消息，扣除活跃度 (扣减值为 0 时不实际扣减)

        从未发言用户（无 activity key）跳过记录，保持活跃度 0 以维持新人非文本拦截。

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID

        Returns:
            更新后的活跃度
        """
        redis = get_redis()
        activity_key = RedisKeys.user_activity(chat_id, user_id)
        last_date_key = RedisKeys.activity_last_date(chat_id, user_id)

        # 从未发言用户（无 activity key）不建立活跃度记录：
        # 非文本消息不扣分，也不应创建 key=0，否则会被衰减下限误判为
        # "曾发言"而绕过新人非文本拦截，保持活跃度 0（新人状态）。
        if await redis.get(activity_key) is None:
            return 0

        # 先获取当前实际活跃度 (含衰减)
        current = await ActivityService.get_activity(chat_id, user_id)

        # 扣除活跃度
        new_activity = current - ActivityService.NON_TEXT_PENALTY

        # ✅ 下限检查：活跃度不低于 0
        if new_activity < 0:
            new_activity = 0

        # 更新 Redis
        await redis.set(activity_key, str(new_activity))
        await redis.set(last_date_key, date.today().isoformat())

        logger.debug(f"活跃度减少 [群组:{chat_id}] [用户:{user_id}] {current} -> {new_activity}")

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
        reduction = 0.05 * math.log2(activity / 10.0)

        # 限制最大减少值
        max_reduction = settings.activity_max_confidence_reduction
        return min(reduction, max_reduction)
