"""宵禁模式服务模块"""

from datetime import datetime, timedelta

from src.core.redis import RedisKeys, get_redis
from src.core.utils import utcnow
from src.models.group import Group


class CurfewService:
    """宵禁模式服务"""

    @staticmethod
    def get_local_time(timezone_offset: int) -> datetime:
        """获取本地时间（应用时区偏移）

        Args:
            timezone_offset: 时区偏移（相对UTC的小时数）

        Returns:
            本地时间
        """
        utc_now = utcnow()
        return utc_now + timedelta(hours=timezone_offset)

    @staticmethod
    def is_in_curfew(group: Group, current_time: datetime | None = None) -> bool:
        """检查当前时间是否在宵禁期内

        Args:
            group: 群组配置
            current_time: 当前时间（用于测试），None则使用实际本地时间

        Returns:
            是否在宵禁期内
        """
        if not group.curfew_enabled:
            return False

        if group.curfew_start_hour is None or group.curfew_end_hour is None:
            return False

        if current_time is None:
            current_time = CurfewService.get_local_time(group.curfew_timezone_offset)

        # 将时间转换为分钟数进行比较
        current_minutes = current_time.hour * 60 + current_time.minute
        start_minutes = group.curfew_start_hour * 60 + group.curfew_start_minute
        end_minutes = group.curfew_end_hour * 60 + group.curfew_end_minute

        # 处理跨天情况（例如 23:00-07:00）
        if start_minutes > end_minutes:
            # 宵禁跨越午夜
            return current_minutes >= start_minutes or current_minutes < end_minutes
        else:
            # 正常同一天宵禁
            return start_minutes <= current_minutes < end_minutes

    @staticmethod
    async def check_message_allowed(
        group: Group, user_activity: int, is_text_message: bool
    ) -> tuple[bool, str | None]:
        """检查消息是否允许发送

        Args:
            group: 群组配置
            user_activity: 用户活跃度
            is_text_message: 是否为文本消息

        Returns:
            (是否允许, 拒绝原因)
        """
        if not CurfewService.is_in_curfew(group):
            return True, None

        # 活跃度 = 0: 阻止所有消息
        if user_activity == 0:
            return False, "宵禁期间活跃度为 0，无法发送任何消息"

        # 活跃度 < 10: 阻止非文本消息
        if user_activity < 10 and not is_text_message:
            return False, f"宵禁期间活跃度不足 10（当前 {user_activity}），无法发送非文本消息"

        return True, None

    @staticmethod
    async def track_curfew_state(chat_id: int, is_in_curfew: bool) -> tuple[bool, bool]:
        """跟踪宵禁状态变化

        Args:
            chat_id: 群组ID
            is_in_curfew: 当前是否在宵禁期

        Returns:
            (是否刚进入宵禁, 是否刚退出宵禁)
        """
        redis = get_redis()
        state_key = RedisKeys.curfew_state(chat_id)

        # 获取之前的状态
        prev_state = await redis.get(state_key)
        current_state = "in" if is_in_curfew else "out"

        # 检测转换
        entered = prev_state == "out" and current_state == "in"
        exited = prev_state == "in" and current_state == "out"

        # 更新状态（TTL 25 小时）
        await redis.setex(state_key, 90000, current_state)

        return entered, exited
