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
    def _hint_flow(flow: str) -> str:
        """校验验证引导 flow（三个 hint 键共用，防止拼出无归属的键名）"""
        if flow not in ("join", "join_request"):
            raise ValueError(f"不支持的验证引导 flow: {flow}")
        return flow

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
    def spam_review(chat_id: int, orig_msg_id: int) -> str:
        """垃圾消息人工复核状态键名

        存储 SpamReviewState v1 JSON（create_review_state 用 SET NX EX 写入）。
        """
        return f"spam_review:{chat_id}:{orig_msg_id}"

    @staticmethod
    def spam_review_lock(chat_id: int, orig_msg_id: int) -> str:
        """垃圾消息复核处理锁键名

        防止同一原始消息的复核 callback 并发执行（review_lock 取锁）。
        """
        return f"spam_review_lock:{chat_id}:{orig_msg_id}"

    @staticmethod
    def verification_hint(chat_id: int, flow: str) -> str:
        """验证引导消息记录键名（join / join_request 各自独立状态）。

        两种验证流程的后续动作不同（join 可恢复 challenge；join_request 立即 decline
        + clear），文案也不同，共用 key 会让先到 flow 的错误文案压住另一个。
        """
        return f"verification_hint:{RedisKeys._hint_flow(flow)}:{chat_id}"

    @staticmethod
    def verification_hint_users(chat_id: int, flow: str) -> str:
        """引导消息待 mention 用户键名（ZSET：member=user_id，score=加入序号）。

        存放当前引导窗口内「未启动 Bot、仍在等待验证」的用户，供引导消息渲染
        匿名 mention。生命周期严格跟随 :meth:`verification_hint`：新窗口取得
        发送权时清空，窗口续期时同步续期，故不会把上一窗口的用户带进新消息。
        """
        return f"verification_hint_users:{RedisKeys._hint_flow(flow)}:{chat_id}"

    @staticmethod
    def verification_hint_render(chat_id: int, flow: str) -> str:
        """引导消息 mention 渲染版本键名（String：已渲染的 mention 数）。

        多个晚到用户会各自触发一次编辑，此值作单调递增 CAS 版本，防止先发起
        但后到达的编辑把已含更多 mention 的消息覆盖回旧内容。
        """
        return f"verification_hint_render:{RedisKeys._hint_flow(flow)}:{chat_id}"

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
    def verification_deadline(chat_id: int, user_id: int) -> str:
        """验证截止时间键名

        存储 ``{session_id}:{deadline_epoch_ms}``，TTL = timeout + 10s grace。
        /start 恢复与 timeout claim 据此判断剩余时间与 session 身份一致性。
        """
        return f"verification_deadline:{chat_id}:{user_id}"

    @staticmethod
    def verification_recovery(chat_id: int, user_id: int) -> str:
        """验证 UI delivery/recovery 状态机键名（不按 flow 分键）。

        同一用户同一群组同一时刻只能有一个验证会话；若按 flow 分键，新旧 deep-link
        可分别取锁并覆盖同一答案。状态值四态：

        - ``undelivered:{session_id}``：私聊发送失败（用户未启动 Bot），可经 /start 恢复
        - ``pending:{session_id}:{revision}:{owner_token}``：某协程取得发送权，UI 未提交
        - ``message:{session_id}:{revision}:{flow}:{message_id}``：UI 已发送，可读 message_id
        - ``timeout:{session_id}``：timeout 已 claim，二次 claim 返回 stale（防重复处罚）
        """
        return f"verification_recovery:{chat_id}:{user_id}"

    @staticmethod
    def captcha_waiting(chat_id: int, user_id: int) -> str:
        """验证码输入等待辅助键名（输入模式 UI 状态，非状态机 5 键）。

        值存 ``{session_id}:{message_id}``（按 session deadline 到期）；滚动发布期间可能读到
        旧格式 ``{message_id}``，消费者仅在 message_id 仍匹配当前 recovery UI 时兼容。残留由
        读时校验清理（on_captcha_text_input 校验 session 匹配）。
        """
        return f"captcha_waiting:{chat_id}:{user_id}"

    @staticmethod
    def captcha_waiting_user(user_id: int) -> str:
        """验证码等待反向索引辅助键名（存 ``{chat_id}``，与 captcha_waiting 同 Lua 写入同到期）。"""
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
    def spam_handler_admins(chat_id: int) -> str:
        """具备 spam 处置权限的管理员缓存键名

        存 get_spam_handler_admins_mention 过滤后的管理员 ID 列表（非全部管理员），
        减少 Telegram API 调用。键名含策略语义（spam_handler）：策略过滤条件
        变更时必须换键，避免滚动部署期间新旧进程共用旧语义缓存。
        存储格式: JSON 数组 [{"id": int}, ...]（空列表也缓存，避免反复请求 API）
        TTL: 300 秒（5分钟，权限变更最长延迟与此一致）
        """
        return f"spam_handler_admins:{chat_id}"

    @staticmethod
    def verification_deadline_pattern() -> str:
        """verification_deadline 键的 SCAN 匹配模式

        与 :func:`verification_deadline` 同前缀——启动恢复扫描经此取模式，
        键名格式变更时两处同步修改，避免散落硬编码。
        """
        return "verification_deadline:*"

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

    @staticmethod
    def data_cleanup_last_run() -> str:
        """数据清理最近一次成功运行的时间戳键名

        存储格式: Unix 秒级时间戳（字符串）。用于启动间隔守卫：距上次运行
        过近（crash-loop / 滚动部署背靠背重启）时跳过启动首轮清理。
        TTL: 2 × data_cleanup_interval_hours 小时（仅作清理，判断以时间戳为准）
        """
        return "data_cleanup:last_run"
