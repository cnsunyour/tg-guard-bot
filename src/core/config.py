"""核心配置模块，使用 Pydantic Settings 管理环境变量"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram Bot 配置
    bot_token: str = Field(..., description="Telegram Bot Token")
    admin_ids: list[int] = Field(default_factory=list, description="管理员 ID 列表")

    # 数据库配置
    db_host: str = Field(default="localhost", description="数据库主机")
    db_port: int = Field(default=5432, description="数据库端口")
    db_user: str = Field(default="postgres", description="数据库用户名")
    db_password: str = Field(default="postgres", description="数据库密码")
    db_name: str = Field(default="tg_guard", description="数据库名称")

    # Redis 配置
    redis_host: str = Field(default="localhost", description="Redis 主机")
    redis_port: int = Field(default=6379, description="Redis 端口")
    redis_db: int = Field(default=0, description="Redis 数据库")
    redis_password: str | None = Field(default=None, description="Redis 密码")

    # i18n 多语言配置
    default_locale: str = Field(default="zh-Hans", description="Bot 默认语言（BCP 47）")
    supported_locales: list[str] = Field(
        default_factory=lambda: ["zh-Hans", "zh-Hant", "en"],
        description="Bot 支持的语言列表",
    )
    locale_cache_ttl_seconds: int = Field(
        default=3600, ge=1, description="群组和用户语言偏好缓存时间（秒）"
    )

    # 应用配置
    log_level: str = Field(default="INFO", description="日志级别")
    debug: bool = Field(default=False, description="调试模式")

    # Sentry 配置
    sentry_dsn: str | None = Field(default=None, description="Sentry DSN（用于错误监控）")
    sentry_environment: str = Field(default="production", description="Sentry 环境标识")
    sentry_traces_sample_rate: float = Field(
        default=1.0, description="Sentry 性能监控采样率（0.0-1.0）"
    )

    # 反垃圾配置
    spam_threshold_rule: float = Field(default=0.8, description="规则引擎阈值")
    spam_threshold_ml: float = Field(default=0.7, description="ML 分类器阈值")
    spam_threshold_embedding: float = Field(default=0.75, description="Embedding 阈值")
    spam_high_confidence_threshold: float = Field(
        default=0.9, description="高置信度阈值（>= 此值踢出并封禁，< 此值禁言）"
    )
    spam_min_text_length: int = Field(
        default=10,
        ge=0,
        le=1000,
        description="垃圾检测的最小标准化文本长度，低于此长度的消息跳过检测，设为0禁用。"
        "标准化长度计算：1个汉字/全角字符=1标准长度，2个英文字符=1标准长度。"
        "推荐值：10（中文10个汉字，英文20个字符）",
    )

    # ========== 高级正则规则引擎配置 ==========
    regex_rules_enabled: bool = Field(default=True, description="是否启用高级正则规则引擎")
    regex_rules_config_path: str = Field(
        default="config/spam_rules.json", description="自定义规则配置文件路径"
    )
    regex_rules_max_text_length: int = Field(default=500, description="正则规则检测的最大文本长度")

    # AI 垃圾检测配置（OpenAI 兼容 API）
    ai_spam_enabled: bool = Field(default=False, description="是否启用 AI API 垃圾检测")
    ai_spam_api_key: str = Field(default="", description="AI API Key")
    ai_spam_api_base: str = Field(
        default="https://api.openai.com/v1",
        description="API Base URL（支持 OpenRouter、DeepSeek、Moonshot 等）",
    )
    ai_spam_model: str = Field(default="gpt-4o-mini", description="模型名称")
    ai_spam_threshold: float = Field(default=0.8, description="置信度阈值")
    ai_spam_timeout: int = Field(default=10, description="超时时间（秒）")
    ai_spam_max_retries: int = Field(default=2, description="最大重试次数")
    ai_spam_client_idle_rebuild_minutes: int = Field(
        default=60, ge=1, description="AI HTTP 客户端空闲重建阈值（分钟）"
    )
    ai_spam_client_max_lifetime_hours: int = Field(
        default=24, ge=1, description="AI HTTP 客户端最大存活时间（小时）"
    )
    ai_spam_auto_train: bool = Field(default=True, description="是否自动入库训练")
    ai_spam_auto_train_negatives: bool = Field(
        default=False, description="是否自动入库高置信度负样本（正常消息）"
    )
    ai_spam_negative_threshold: float = Field(
        default=0.2, description="负样本置信度阈值（置信度 <= 此值时入库正常样本）"
    )
    ai_spam_max_length: int = Field(default=500, description="文本最大长度")
    ai_spam_labeled_by: int = Field(default=-1, description="AI 标注者 ID")

    # ========== AI 垃圾检测配置（备份服务商） ==========
    ai_spam_backup_enabled: bool = Field(
        default=False,
        description="是否启用备份 AI 服务商（需要同时启用 AI_SPAM_ENABLED）",
    )
    ai_spam_backup_api_key: str = Field(default="", description="备份 AI API Key")
    ai_spam_backup_api_base: str = Field(
        default="https://api.openai.com/v1",
        description="备份 API Base URL（支持不同提供商）",
    )
    ai_spam_backup_model: str = Field(default="gpt-4o-mini", description="备份模型名称")
    ai_spam_backup_threshold: float = Field(default=0.8, description="备份置信度阈值")
    ai_spam_backup_timeout: int = Field(default=10, description="备份超时时间（秒）")
    ai_spam_backup_max_retries: int = Field(default=2, description="备份最大重试次数")

    # ========== AI Vision 多模态检测配置（图片/贴纸）==========
    # Vision 直判：图片/贴纸直接送多模态 AI 判垃圾（独立于文本检测，可用不同模型）
    ai_spam_vision_enabled: bool = Field(
        default=False,
        description="是否启用 AI Vision 直判图片/贴纸（独立于文本 AI_SPAM_ENABLED；model 须多模态）",
    )
    ai_spam_vision_detail: str = Field(
        default="low",
        description="OpenAI image_url.detail: low(~85 tokens/图) / high(1000+) / auto",
    )
    ai_spam_vision_max_image_bytes: int = Field(
        default=5_242_880,  # 5MB
        ge=1,
        description="Vision 接受的最大图片字节数，超限则跳过该图片检测",
    )
    ai_spam_vision_timeout: int = Field(
        default=30,
        ge=1,
        description="Vision 请求超时时间（秒），通常比文本检测更长",
    )

    # ---- Vision 主服务商（key/base 留空回退文本主配置 ai_spam_*；model 始终独立）----
    ai_spam_vision_api_key: str = Field(
        default="", description="Vision 主服务商 API Key（留空回退 AI_SPAM_API_KEY）"
    )
    ai_spam_vision_api_base: str = Field(
        default="", description="Vision 主服务商 API Base（留空回退 AI_SPAM_API_BASE）"
    )
    ai_spam_vision_model: str = Field(
        default="gpt-4o-mini", description="Vision 主模型名称（必须支持多模态）"
    )
    ai_spam_vision_threshold: float = Field(default=0.8, description="Vision 主置信度阈值")
    ai_spam_vision_max_retries: int = Field(default=2, description="Vision 主最大重试次数")

    # ---- Vision 备服务商（key/base 留空回退文本备配置 ai_spam_backup_*；model 始终独立）----
    ai_spam_vision_backup_enabled: bool = Field(
        default=False, description="是否启用 Vision 备份服务商（需 AI_SPAM_VISION_ENABLED=true）"
    )
    ai_spam_vision_backup_api_key: str = Field(
        default="", description="Vision 备服务商 API Key（留空回退 AI_SPAM_BACKUP_API_KEY）"
    )
    ai_spam_vision_backup_api_base: str = Field(
        default="", description="Vision 备服务商 API Base（留空回退 AI_SPAM_BACKUP_API_BASE）"
    )
    ai_spam_vision_backup_model: str = Field(
        default="gpt-4o-mini", description="Vision 备模型名称（必须支持多模态）"
    )
    ai_spam_vision_backup_threshold: float = Field(default=0.8, description="Vision 备置信度阈值")
    ai_spam_vision_backup_max_retries: int = Field(default=2, description="Vision 备最大重试次数")

    # 上下文检测配置
    context_enabled: bool = Field(default=False, description="是否启用上下文检测（需要 AI 检测）")
    context_message_count: int = Field(default=10, ge=1, description="群组上下文消息数量")
    context_ttl_minutes: int = Field(default=10, ge=1, description="上下文缓存时间（分钟）")
    context_reply_depth: int = Field(default=3, ge=0, description="回复链最大追溯深度")
    context_max_text_length: int = Field(default=200, ge=1, description="单条消息最大文本长度")

    # 上下文一致性检测配置（用于降低误判）
    context_consistency_enabled: bool = Field(
        default=True, description="是否启用上下文一致性检测（用于降低误判）"
    )
    context_high_similarity_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="高相似度阈值（>= 此值视为正常对话）"
    )
    context_confidence_reduction: float = Field(
        default=0.15, ge=0.0, le=1.0, description="上下文一致时降低的置信度"
    )
    reply_similarity_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="回复链相似度阈值（>= 此值视为正常对话）"
    )
    reply_confidence_reduction: float = Field(
        default=0.2, ge=0.0, le=1.0, description="回复内容相关时降低的置信度"
    )

    # 模型自动训练配置
    auto_train_threshold: int = Field(
        default=500, description="触发自动训练的新样本数阈值（默认 500）"
    )
    auto_train_cooldown_hours: int = Field(
        default=168, description="自动训练冷却时间（小时），默认 168 小时（7 天）"
    )

    # Turnstile 验证配置（Cloudflare 无感人机验证）
    turnstile_enabled: bool = Field(default=False, description="是否启用 Turnstile 验证")
    turnstile_site_key: str = Field(default="", description="Turnstile Site Key")
    turnstile_secret_key: str = Field(default="", description="Turnstile Secret Key")

    # ========== 统一 CAPTCHA 验证配置 ==========
    captcha_webapp_url: str = Field(
        default="",
        description="统一 CAPTCHA WebApp URL（支持 Turnstile, Friendly, hCaptcha, MTCaptcha）",
    )
    captcha_signature_key: str = Field(
        default="",
        description="与 CAPTCHA WebApp 共享的签名密钥（用于验证回调数据，至少 32 字符）",
    )

    # Friendly Captcha 验证配置（隐私友好，支持多 key 轮换）
    friendly_enabled: bool = Field(default=False, description="是否启用 Friendly Captcha 验证")
    friendly_keys: str | list[dict] = Field(
        default_factory=list,
        description='Friendly Captcha key pairs for rotation (JSON array: [{"sitekey":"FC...","apikey":"fc-sk-..."}])',
    )

    # hCaptcha 验证配置（图片验证）
    hcaptcha_enabled: bool = Field(default=False, description="是否启用 hCaptcha 验证")
    hcaptcha_site_key: str = Field(default="", description="hCaptcha Site Key")
    hcaptcha_secret_key: str = Field(default="", description="hCaptcha Secret Key")

    # MTCaptcha 验证配置（自适应无感验证）
    mtcaptcha_enabled: bool = Field(default=False, description="是否启用 MTCaptcha 验证")
    mtcaptcha_site_key: str = Field(default="", description="MTCaptcha Site Key")
    mtcaptcha_private_key: str = Field(default="", description="MTCaptcha Private Key")

    # ALTCHA 验证配置（开源自托管，proof-of-work）
    altcha_enabled: bool = Field(default=False, description="是否启用 ALTCHA 验证")
    altcha_api_url: str = Field(
        default="",
        description="ALTCHA PHP 后端 URL（如 https://xxx.serv00.net/altcha）",
    )
    altcha_hmac_key: str = Field(
        default="",
        description="ALTCHA HMAC key（与 PHP 后端共享，用于挑战生成和验证）",
    )

    # 验证配置
    verification_timeout: int = Field(default=120, description="验证超时时间(秒)")
    verification_inflight_ttl_seconds: int = Field(
        default=300,
        ge=1,
        description=(
            "入群请求/入群事件处理中锁的 TTL（秒）。正常处理结束后立即释放；"
            "此值仅用于进程异常退出等场景的死锁兜底，应大于单次处理的最坏耗时"
            "（AI 检测主备链路串行 × 重试）。默认 300 秒。"
        ),
    )
    verification_joining_window_seconds: int = Field(
        default=3,
        ge=1,
        description=(
            "入群短窗口消息删除时长（秒）：新成员在此时长内于群里发言，消息会被直接删除，"
            "用于拦截 restrict 生效前的抢发。默认 3 秒，依据 restrict 往返 + 消息投递延迟估算；"
            "只删不封，误删代价小故偏长优于偏短。"
        ),
    )
    max_warnings: int = Field(default=3, description="最大警告次数（触发禁言）")
    warning_expiration_days: int = Field(default=7, description="警告有效期（天）")
    warning_mute_duration_hours: int = Field(
        default=24, description="警告达到阈值后的禁言时长（小时）"
    )
    warning_kick_threshold: int = Field(default=5, description="踢出群组阈值（次）")
    warning_ban_threshold: int = Field(default=7, description="封禁阈值（次，踢出+拉黑）")

    # CAS (Combot Anti-Spam) API 配置
    cas_enabled: bool = Field(
        default=False,
        description="是否启用 CAS 黑名单检查（启用后自动拦截入群和消息）",
    )
    cas_api_url: str = Field(default="https://api.cas.chat", description="CAS API 基础 URL")
    cas_check_timeout: int = Field(default=5, description="API 请求超时时间（秒）")
    cas_cache_ttl: int = Field(
        default=86400,
        description="检查结果缓存时间（秒），默认 24 小时",
    )
    cas_max_retries: int = Field(default=2, description="API 请求最大重试次数")

    # 用户状态检测配置（基于 Telethon）
    user_status_check_enabled: bool = Field(
        default=False,
        description="是否启用用户状态检测（需要启用 Telethon，检测 restricted/scam/fake/deleted 用户）",
    )
    user_status_cache_ttl: int = Field(
        default=3600,
        description="用户状态检查结果缓存时间（秒），默认 1 小时",
    )
    user_status_max_retries: int = Field(default=2, description="用户状态检查最大重试次数")

    # 活跃度系统配置
    activity_max_confidence_reduction: float = Field(
        default=0.15, description="活跃度最大置信度减少值（用于反垃圾检测）"
    )
    activity_skip_spam_check_threshold: int = Field(
        default=0,
        description="活跃度跳过垃圾检测全局阈值（>0=全局统一阈值，=0=使用群组配置，<0=全局禁用）",
    )
    activity_decay_floor: int = Field(
        default=1,
        ge=0,
        description="活跃度衰减下限：曾经发过言的用户活跃度最多衰减到此值（默认1，使其免受非文本消息拦截误伤）；设为0则退回旧行为（可衰减到0）。",
    )

    # AI 模型路径
    ml_model_path: str = Field(default="data/models/spam_classifier.pkl", description="ML 模型路径")
    embedding_model_name: str = Field(
        default="BAAI/bge-small-zh-v1.5", description="Embedding 模型名称"
    )

    # Telethon 配置（用于获取群组成员列表）
    telethon_api_id: int | None = Field(default=None, description="Telegram API ID")
    telethon_api_hash: str | None = Field(default=None, description="Telegram API Hash")
    telethon_session_path: str = Field(
        default="./data/user_bot.session", description="Telethon Session 文件路径"
    )
    telethon_enabled: bool = Field(default=False, description="是否启用 Telethon 客户端")
    cleanup_cache_ttl: int = Field(default=3600, description="成员列表缓存时间（秒）")
    # ✅ P1-9: 模型签名密钥改为必填，强制用户配置安全密钥
    model_signature_key: str = Field(
        ...,
        description="模型文件签名密钥（必填：防止模型文件被篡改，请使用随机生成的密钥）",
        min_length=32,  # 要求至少 32 个字符以确保安全性
    )
    # 🔒 安全：是否允许加载未签名的旧模型（默认禁止，防止 RCE 攻击）
    allow_unsigned_models: bool = Field(
        default=False,
        description="是否允许加载未签名的旧版模型（不安全，仅用于兼容旧模型，强烈建议重新训练）",
    )

    @field_validator("redis_password", "sentry_dsn", "telethon_api_hash", mode="before")
    @classmethod
    def parse_optional_str(cls, v: str | None) -> str | None:
        """解析可选字符串字段，将空字符串转换为 None"""
        if v == "":
            return None
        return v

    @field_validator("telethon_api_id", mode="before")
    @classmethod
    def parse_telethon_api_id(cls, v: str | int | None) -> int | None:
        """解析 Telethon API ID，允许空字符串"""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return int(v)
        return v

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: str | list[int]) -> list[int]:
        """解析管理员 ID 列表"""
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("ai_spam_vision_detail", mode="after")
    @classmethod
    def validate_vision_detail(cls, v: str) -> str:
        """校验 Vision 的 detail 取值"""
        allowed = {"low", "high", "auto"}
        if v not in allowed:
            raise ValueError(f"ai_spam_vision_detail 必须是 {allowed} 之一，当前: {v}")
        return v

    @field_validator("friendly_keys", mode="before")
    @classmethod
    def parse_friendly_keys(cls, v: str | list[dict]) -> list[dict]:
        """解析 Friendly Captcha keys JSON 数组"""
        if isinstance(v, str):
            if not v.strip():
                return []
            import json

            try:
                parsed = json.loads(v)
                if not isinstance(parsed, list):
                    raise ValueError("FRIENDLY_KEYS must be a JSON array")
                return parsed
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in FRIENDLY_KEYS: {e}") from e
        return v

    def model_post_init(self, __context) -> None:
        """模型初始化后验证生产环境安全配置

        ✅ 安全修复：强制生产环境使用安全密码
        """
        if not self.debug:
            # 检查数据库密码
            if (
                self.db_password == "postgres"
            ):  # nosec B105 - 这是检查默认密码的安全检查,非硬编码密码
                raise ValueError(
                    "🔒 生产环境禁止使用默认数据库密码！\n"
                    "请在 .env 文件中设置安全的 DB_PASSWORD\n"
                    "建议：使用至少 16 位的随机密码"
                )

            # 检查 Redis 密码
            if not self.redis_password:
                raise ValueError(
                    "🔒 生产环境必须设置 Redis 密码！\n"
                    "请在 .env 文件中设置 REDIS_PASSWORD\n"
                    "建议：使用至少 16 位的随机密码"
                )

        # Vision 备份依赖 Vision 主开关；不一致则提示（不强制关闭）
        if self.ai_spam_vision_backup_enabled and not self.ai_spam_vision_enabled:
            import warnings

            warnings.warn(
                "ai_spam_vision_backup_enabled=true 但 ai_spam_vision_enabled=false，"
                "Vision 备份不会生效",
                stacklevel=2,
            )

    @property
    def database_url(self) -> str:
        """生成数据库 URL"""
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def redis_url(self) -> str:
        """生成 Redis URL"""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def vision_api_key_effective(self) -> str:
        """Vision 主 API Key：留空回退文本主 key"""
        return self.ai_spam_vision_api_key or self.ai_spam_api_key

    @property
    def vision_api_base_effective(self) -> str:
        """Vision 主 API Base：留空回退文本主 base"""
        return self.ai_spam_vision_api_base or self.ai_spam_api_base

    @property
    def vision_backup_api_key_effective(self) -> str:
        """Vision 备 API Key：留空回退文本备 key"""
        return self.ai_spam_vision_backup_api_key or self.ai_spam_backup_api_key

    @property
    def vision_backup_api_base_effective(self) -> str:
        """Vision 备 API Base：留空回退文本备 base"""
        return self.ai_spam_vision_backup_api_base or self.ai_spam_backup_api_base


# 全局配置实例
# mypy 不理解 pydantic-settings 会从环境变量读取必需字段
settings = Settings()  # type: ignore[call-arg]
