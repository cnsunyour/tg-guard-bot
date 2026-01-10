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

    # AI 垃圾检测配置（OpenAI 兼容 API）
    ai_spam_enabled: bool = Field(default=False, description="是否启用 AI API 垃圾检测")
    ai_spam_api_key: str = Field(default="", description="AI API Key")
    ai_spam_api_base: str = Field(
        default="https://api.openai.com/v1",
        description="API Base URL（支持 OpenRouter、DeepSeek、Moonshot 等）",
    )
    ai_spam_model: str = Field(default="gpt-4o-mini", description="模型名称")
    ai_spam_temperature: float = Field(default=0.0, description="生成温度")
    ai_spam_threshold: float = Field(default=0.8, description="置信度阈值")
    ai_spam_timeout: int = Field(default=10, description="超时时间（秒）")
    ai_spam_max_retries: int = Field(default=2, description="最大重试次数")
    ai_spam_auto_train: bool = Field(default=True, description="是否自动入库训练")
    ai_spam_max_length: int = Field(default=500, description="文本最大长度")
    ai_spam_labeled_by: int = Field(default=-1, description="AI 标注者 ID")

    # 验证配置
    verification_timeout: int = Field(default=120, description="验证超时时间(秒)")
    max_warnings: int = Field(default=3, description="最大警告次数（触发禁言）")
    warning_expiration_days: int = Field(default=7, description="警告有效期（天）")
    warning_mute_duration_hours: int = Field(
        default=24, description="警告达到阈值后的禁言时长（小时）"
    )
    warning_kick_threshold: int = Field(default=5, description="踢出群组阈值（次）")
    warning_ban_threshold: int = Field(default=7, description="封禁阈值（次，踢出+拉黑）")

    # 活跃度系统配置
    activity_enabled: bool = Field(default=True, description="是否启用活跃度系统")
    activity_max_confidence_reduction: float = Field(
        default=0.15, description="活跃度最大置信度减少值（用于反垃圾检测）"
    )
    activity_skip_spam_check_threshold: int = Field(
        default=0,
        description="活跃度跳过垃圾检测全局阈值（>0=全局统一阈值，=0=使用群组配置，<0=全局禁用）",
    )

    # AI 模型路径
    ml_model_path: str = Field(default="data/models/spam_classifier.pkl", description="ML 模型路径")
    embedding_model_name: str = Field(
        default="BAAI/bge-small-zh-v1.5", description="Embedding 模型名称"
    )
    # OCR 功能配置
    enable_ocr: bool = Field(
        default=False, description="是否启用 OCR 功能（需要 4GB+ RAM，ARM 架构可能不稳定）"
    )
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

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: str | list[int]) -> list[int]:
        """解析管理员 ID 列表"""
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
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


# 全局配置实例
settings = Settings()
