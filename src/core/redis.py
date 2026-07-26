"""Redis 连接管理模块"""

from redis.asyncio import ConnectionPool, Redis

from src.core.config import settings

# 全局 Redis 连接池和客户端
_redis_pool: ConnectionPool | None = None
_redis_client: Redis | None = None


def get_redis_pool() -> ConnectionPool:
    """获取 Redis 连接池"""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = ConnectionPool.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


def get_redis() -> Redis:
    """获取 Redis 客户端"""
    global _redis_client
    if _redis_client is None:
        pool = get_redis_pool()
        _redis_client = Redis(connection_pool=pool)
    return _redis_client


async def close_redis() -> None:
    """关闭 Redis 连接"""
    global _redis_client, _redis_pool
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
    if _redis_pool:
        await _redis_pool.disconnect()
        _redis_pool = None


class RedisKeys:
    """Redis 键名常量"""

    @staticmethod
    def verification(chat_id: int, user_id: int) -> str:
        """验证码键名"""
        return f"verification:{chat_id}:{user_id}"

    @staticmethod
    def verification_joining(chat_id: int, user_id: int) -> str:
        """入群短窗口标记键名

        on_user_join 绝对第一步写入，用于拦截 restrict_chat_member 生效前的抢发消息。
        对所有入群者统一适用，靠 TTL 自动过期（不区分邀请来源、不在任何路径显式清理）。
        TTL 由 settings.verification_joining_window_seconds 控制（默认 3 秒）。
        """
        return f"verification_joining:{chat_id}:{user_id}"

    @staticmethod
    def rate_limit(user_id: int, chat_id: int) -> str:
        """频率限制键名"""
        return f"rate_limit:{chat_id}:{user_id}"

    @staticmethod
    def group_config(chat_id: int) -> str:
        """群组配置缓存键名"""
        return f"group_config:{chat_id}"

    @staticmethod
    def locale_group(chat_id: int) -> str:
        """群组语言缓存键名

        存储 BCP 47 语言代码（如 zh-Hans/zh-Hant/en）。
        TTL: settings.locale_cache_ttl_seconds。
        """
        return f"locale:group:{chat_id}"

    @staticmethod
    def locale_user(user_id: int) -> str:
        """用户语言缓存键名

        存储用户私聊语言解析结果。即使数据库无显式设置，也会缓存默认
        语言（或无记录哨兵）以避免持续穿透数据库。
        TTL: settings.locale_cache_ttl_seconds。
        """
        return f"locale:user:{user_id}"

    @staticmethod
    def user_warnings(chat_id: int, user_id: int) -> str:
        """用户警告计数键名"""
        return f"warnings:{chat_id}:{user_id}"

    @staticmethod
    def spam_message_text(chat_id: int, message_id: int) -> str:
        """垃圾消息文本缓存键名

        ✅ P1-12: 缓存垃圾消息原始文本，用于管理员反馈
        """
        return f"spam_text:{chat_id}:{message_id}"

    @staticmethod
    def verification_hint(chat_id: int) -> str:
        """验证引导消息记录键名

        用于防止多用户同时未启动 Bot 时重复发送引导消息
        """
        return f"verification_hint:{chat_id}"

    @staticmethod
    def verification_approved(chat_id: int, user_id: int) -> str:
        """验证已批准标记键名

        用于标记已通过验证的用户，避免批准加入后重复验证
        """
        return f"verification_approved:{chat_id}:{user_id}"

    @staticmethod
    def verification_type(chat_id: int, user_id: int) -> str:
        """验证类型标记键名

        用于标记验证是加入请求验证(join_request)还是正常验证(normal)
        """
        return f"verification_type:{chat_id}:{user_id}"

    @staticmethod
    def captcha_waiting(chat_id: int, user_id: int) -> str:
        """验证码输入等待状态键名

        用于标记用户正在等待输入验证码，存储验证消息 ID
        """
        return f"captcha_waiting:{chat_id}:{user_id}"

    @staticmethod
    def captcha_waiting_user(user_id: int) -> str:
        """验证码等待反向索引键名

        用于从用户 ID 快速查找等待的验证码所属群组，避免 Redis SCAN 操作
        存储格式: "{chat_id}"
        """
        return f"captcha_waiting_user:{user_id}"

    @staticmethod
    def user_activity(chat_id: int, user_id: int) -> str:
        """用户活跃度键名

        存储用户的活跃度分数（整数），用于限制非文本消息发送
        """
        return f"activity:{chat_id}:{user_id}"

    @staticmethod
    def activity_last_date(chat_id: int, user_id: int) -> str:
        """用户最后消息日期键名

        存储最后发送消息的日期（YYYY-MM-DD），用于每日衰减计算
        """
        return f"activity_date:{chat_id}:{user_id}"

    @staticmethod
    def turnstile_token(chat_id: int, user_id: int) -> str:
        """Turnstile 验证 token 键名

        用于存储一次性验证 token,防止重放攻击
        存储格式: "{token_string}"
        """
        return f"turnstile_token:{chat_id}:{user_id}"

    @staticmethod
    def captcha_token(chat_id: int, user_id: int) -> str:
        """通用 CAPTCHA 验证 token 键名

        用于存储所有 CAPTCHA 服务（Friendly, hCaptcha, MTCaptcha, ALTCHA）的一次性验证 token
        存储格式: "{provider}:{token_string}:{key_index}"
        示例: "friendly:abc123:0" 或 "hcaptcha:xyz789"
        """
        return f"captcha_token:{chat_id}:{user_id}"

    @staticmethod
    def friendly_key_index() -> str:
        """Friendly Captcha key 轮换索引键名

        使用 Redis INCR 原子操作实现 round-robin key 轮换
        存储格式: 整数索引（自动递增）
        """
        return "friendly_captcha:key_index"

    @staticmethod
    def cleanup_members_cache(chat_id: int) -> str:
        """群组成员缓存键名

        用于缓存 Telethon 获取的群组成员列表，减少 API 调用
        存储格式: JSON 字符串 {"chat_id": int, "cached_at": str, "members": []}
        TTL: 1 小时（可配置）
        """
        return f"cleanup:members:{chat_id}"

    @staticmethod
    def last_train_time() -> str:
        """上次模型训练时间键名

        用于实现自动训练冷却时间
        存储格式: Unix 时间戳（秒）
        """
        return "ml:last_train_time"

    @staticmethod
    def group_context(chat_id: int) -> str:
        """群组上下文消息缓存键名

        存储群组最近 N 条消息，用于上下文检测
        存储格式: Redis List，每个元素是 JSON 字符串 {"user_id": int, "user_name": str, "text": str, "timestamp": int, "message_id": int}
        TTL: 可配置（默认 10 分钟）
        """
        return f"context:group:{chat_id}"

    @staticmethod
    def username_mapping(username: str) -> str:
        """全局 username → user_id 映射

        格式: "username_map:{username.lower()}"
        存储: user_id (整数)
        TTL: 7 天

        注意：
        - username 不区分大小写，统一转为小写
        - 全局唯一，不区分群组
        """
        return f"username_map:{username.lower()}"

    @staticmethod
    def cas_result(user_id: int) -> str:
        """CAS 检查结果缓存键名

        存储格式: JSON 字符串（原始 API 响应）
        - 黑名单: {"ok": true, "result": {"offenses": 3, "time_added": 1234567890}}
        - 正常: {"ok": false, "description": "Record not found."}
        TTL: 可配置（默认 24 小时）
        """
        return f"cas:result:{user_id}"

    @staticmethod
    def cas_lock(user_id: int) -> str:
        """CAS 检查分布式锁键名

        用于防止同一个用户的并发 CAS API 请求
        存储格式: "1" (表示正在检查)
        TTL: 10 秒（防止死锁）
        """
        return f"cas:lock:{user_id}"

    @staticmethod
    def chat_admins(chat_id: int) -> str:
        """群组管理员列表缓存键名

        用于缓存群组管理员列表，减少 Telegram API 调用
        存储格式: JSON 数字符串 [{"id": int, "full_name": str}, ...]
        TTL: 300 秒（5分钟）
        """
        return f"chat_admins:{chat_id}"

    @staticmethod
    def curfew_state(chat_id: int) -> str:
        """宵禁状态键名

        用于跟踪群组当前是否处于宵禁期，检测进入/退出转换
        存储格式: "in" (宵禁中) 或 "out" (非宵禁)
        TTL: 25 小时（覆盖每日周期）
        """
        return f"curfew_state:{chat_id}"

    @staticmethod
    def join_request_dedup(chat_id: int, user_id: int) -> str:
        """加入请求去重键名

        防止用户连续多次点击申请加入触发重复处理
        TTL: 60 秒
        """
        return f"join_request_dedup:{chat_id}:{user_id}"

    @staticmethod
    def join_request_inflight(chat_id: int, user_id: int) -> str:
        """加入请求处理中锁键名

        防止同一用户的加入请求处理流程并发重入，覆盖 CAS/Telethon 状态/AI 等
        慢检测整段窗口（这些步骤耗时可能远超 60 秒 dedup）。与 join_inflight
        使用独立键，避免批准加入后紧随触发的正常入群事件被误拦截。
        TTL: settings.verification_inflight_ttl_seconds（默认 300 秒）
        """
        return f"join_request_inflight:{chat_id}:{user_id}"

    @staticmethod
    def join_inflight(chat_id: int, user_id: int) -> str:
        """用户入群事件处理中锁键名

        防止同一用户的入群事件（chat_member 更新）处理流程并发重入。
        TTL: settings.verification_inflight_ttl_seconds（默认 300 秒）
        """
        return f"join_inflight:{chat_id}:{user_id}"

    @staticmethod
    def user_status_result(user_id: int) -> str:
        """用户状态检查结果缓存键名

        存储格式: JSON 字符串 {"is_problematic": bool, "reason": str | None, "checked_at": str}
        - is_problematic: 是否为异常用户
        - reason: 异常原因（restricted/scam/fake/deleted）
        - checked_at: 检查时间（ISO 格式）
        TTL: 可配置（默认 1 小时）
        """
        return f"user_status:result:{user_id}"

    @staticmethod
    def user_status_lock(user_id: int) -> str:
        """用户状态检查分布式锁键名

        用于防止同一个用户的并发 Telethon API 请求
        存储格式: "1" (表示正在检查)
        TTL: 10 秒（防止死锁）
        """
        return f"user_status:lock:{user_id}"
