"""配置加载测试"""

import pytest
from pydantic import ValidationError


@pytest.mark.unit
def test_config_from_env(monkeypatch):
    """测试从环境变量加载配置"""
    # 设置必需的环境变量
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    monkeypatch.setenv("ADMIN_IDS", "[123456789, 987654321]")  # JSON 数组格式
    monkeypatch.setenv("DB_PASSWORD", "test_password_12345")
    monkeypatch.setenv("REDIS_PASSWORD", "redis_test_password")
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "a" * 64)  # 至少 32 字符

    from src.core.config import Settings

    settings = Settings()

    assert settings.bot_token == "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
    assert settings.admin_ids == [123456789, 987654321]
    assert settings.db_password == "test_password_12345"
    assert settings.redis_password == "redis_test_password"
    assert len(settings.model_signature_key) >= 32


@pytest.mark.unit
def test_config_model_signature_key_required(monkeypatch):
    """测试模型签名密钥是必填字段"""
    # 设置其他必填字段
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    monkeypatch.setenv("ADMIN_IDS", "[123456789]")
    monkeypatch.setenv("DB_PASSWORD", "test_password")
    monkeypatch.setenv("REDIS_PASSWORD", "redis_password")
    # 设置空的 MODEL_SIGNATURE_KEY 以覆盖 .env 文件中的值
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "")

    from src.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    # 验证错误信息包含 model_signature_key
    errors = exc_info.value.errors()
    assert any("model_signature_key" in str(e) for e in errors)


@pytest.mark.unit
def test_config_model_signature_key_min_length(monkeypatch):
    """测试模型签名密钥最小长度验证"""
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    monkeypatch.setenv("ADMIN_IDS", "[123456789]")
    monkeypatch.setenv("DB_PASSWORD", "test_password")
    monkeypatch.setenv("REDIS_PASSWORD", "redis_password")
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "short")  # 少于 32 字符

    from src.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    errors = exc_info.value.errors()
    assert any("at least 32 characters" in str(e) or "min_length" in str(e) for e in errors)


@pytest.mark.unit
def test_config_default_values(monkeypatch):
    """测试配置默认值"""
    # 清除测试环境变量
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("TESTING", raising=False)

    # 只设置必需字段
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    monkeypatch.setenv("ADMIN_IDS", "[123456789]")
    monkeypatch.setenv("DB_PASSWORD", "test_password")
    monkeypatch.setenv("REDIS_PASSWORD", "redis_password")
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "a" * 64)
    # 断言涉及的部署值显式注入（历史上隐式依赖本地 .env，无 .env 的 CI 环境会挂）
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("VERIFICATION_TIMEOUT", "60")

    from src.core.config import Settings

    settings = Settings()

    # 验证默认值
    assert settings.log_level in ["INFO", "DEBUG"]  # 可能受测试环境影响
    assert settings.db_host == "postgres"
    assert settings.db_port == 5432
    assert settings.db_name == "tg_guard"
    assert settings.redis_host == "redis"
    assert settings.redis_port == 6379
    assert settings.verification_timeout == 60


@pytest.mark.unit
def test_config_regex_rule_settings_from_env(monkeypatch):
    """REGEX_RULES_* 环境变量正确解析；MAX_TEXT_LENGTH 拒绝 <1 的非法值"""
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "a" * 64)
    monkeypatch.setenv("DEBUG", "true")  # 跳过 db/redis 密码校验
    monkeypatch.setenv("REGEX_RULES_ENABLED", "false")
    monkeypatch.setenv("REGEX_RULES_CONFIG_PATH", "config/custom_rules.json")
    monkeypatch.setenv("REGEX_RULES_MAX_TEXT_LENGTH", "750")

    from src.core.config import Settings

    settings = Settings()

    assert settings.regex_rules_enabled is False
    assert settings.regex_rules_config_path == "config/custom_rules.json"
    assert settings.regex_rules_max_text_length == 750

    # ge=1 校验：0 应被拒绝
    monkeypatch.setenv("REGEX_RULES_MAX_TEXT_LENGTH", "0")
    with pytest.raises(ValidationError):
        Settings()


# ===== M4: CAPTCHA_SIGNATURE_KEY 条件校验（配 webapp_url 时强制）=====


def _captcha_env(monkeypatch, *, webapp_url: str | None, signature_key: str) -> None:
    """配置 captcha 相关环境变量（_env_file=None 隔离 .env 干扰）。"""
    monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "a" * 64)
    monkeypatch.setenv("DEBUG", "true")  # 跳过 db/redis 密码校验，聚焦 captcha
    if webapp_url is None:
        monkeypatch.delenv("CAPTCHA_WEBAPP_URL", raising=False)
    else:
        monkeypatch.setenv("CAPTCHA_WEBAPP_URL", webapp_url)
    monkeypatch.setenv("CAPTCHA_SIGNATURE_KEY", signature_key)


@pytest.mark.unit
def test_config_captcha_signature_key_required_with_webapp(monkeypatch):
    """M4：配了 CAPTCHA_WEBAPP_URL 时，空 CAPTCHA_SIGNATURE_KEY → 拒绝启动。"""
    _captcha_env(monkeypatch, webapp_url="https://captcha.example.com", signature_key="")

    from src.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert "CAPTCHA_SIGNATURE_KEY" in str(exc_info.value)


@pytest.mark.unit
def test_config_captcha_signature_key_short_with_webapp(monkeypatch):
    """M4：CAPTCHA_SIGNATURE_KEY < 32 字符 + webapp_url → 拒绝。"""
    _captcha_env(monkeypatch, webapp_url="https://captcha.example.com", signature_key="short")

    from src.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.unit
def test_config_captcha_signature_key_valid_with_webapp(monkeypatch):
    """M4：webapp_url + 32 字符 signature_key → 通过（含 turnstile_enabled=false，P1-2）。"""
    _captcha_env(monkeypatch, webapp_url="https://captcha.example.com", signature_key="s" * 32)

    from src.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.captcha_webapp_url == "https://captcha.example.com"
    assert settings.turnstile_enabled is False  # 显式选择 turnstile 仍受 signature_key 保护


@pytest.mark.unit
def test_config_no_webapp_allows_empty_captcha_signature_key(monkeypatch):
    """M4：未配 webapp_url 时，空 CAPTCHA_SIGNATURE_KEY 可启动（默认场景）。"""
    _captcha_env(monkeypatch, webapp_url=None, signature_key="")

    from src.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.captcha_signature_key == ""


# ===== TypeSafe Jev（typesafe_systemone）协议校验 =====


@pytest.mark.unit
def test_config_accepts_typesafe_systemone_as_text_protocol(monkeypatch):
    """typesafe_systemone 可作文本主/备协议；Vision 未启用时不受影响。"""
    _captcha_env(monkeypatch, webapp_url=None, signature_key="")
    monkeypatch.setenv("AI_SPAM_PROTOCOL", "TypeSafe_SystemOne")
    monkeypatch.setenv("AI_SPAM_BACKUP_PROTOCOL", "typesafe_systemone")
    monkeypatch.setenv("AI_SPAM_VISION_ENABLED", "false")

    from src.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.ai_spam_protocol == "typesafe_systemone"
    assert settings.ai_spam_backup_protocol == "typesafe_systemone"
    # 留空继承到 Jev 本身合法，只有 Vision 启用时才拒绝
    assert settings.vision_protocol_effective == "typesafe_systemone"


@pytest.mark.unit
@pytest.mark.parametrize("env_name", ["AI_SPAM_VISION_PROTOCOL", "AI_SPAM_VISION_BACKUP_PROTOCOL"])
def test_config_rejects_explicit_typesafe_vision_protocol(monkeypatch, env_name):
    """Vision 协议字段显式填 Jev → 字段校验拒绝（不论 Vision 是否启用）。"""
    _captcha_env(monkeypatch, webapp_url=None, signature_key="")
    monkeypatch.setenv(env_name, "typesafe_systemone")

    from src.core.config import Settings

    with pytest.raises(ValidationError, match="Jev 仅支持纯文本"):
        Settings(_env_file=None)


@pytest.mark.unit
def test_config_rejects_typesafe_vision_inheritance_when_vision_enabled(monkeypatch):
    """文本主协议 Jev + Vision 启用 + Vision 协议留空 → 启动拒绝，提示显式配置。"""
    _captcha_env(monkeypatch, webapp_url=None, signature_key="")
    monkeypatch.setenv("AI_SPAM_PROTOCOL", "typesafe_systemone")
    monkeypatch.setenv("AI_SPAM_VISION_ENABLED", "true")

    from src.core.config import Settings

    with pytest.raises(ValidationError, match="AI_SPAM_VISION_PROTOCOL"):
        Settings(_env_file=None)


@pytest.mark.unit
def test_config_rejects_typesafe_vision_backup_inheritance_when_enabled(monkeypatch):
    """文本备协议 Jev + Vision 主备均启用 + Vision 备协议留空 → 启动拒绝。"""
    _captcha_env(monkeypatch, webapp_url=None, signature_key="")
    monkeypatch.setenv("AI_SPAM_BACKUP_PROTOCOL", "typesafe_systemone")
    monkeypatch.setenv("AI_SPAM_VISION_ENABLED", "true")
    monkeypatch.setenv("AI_SPAM_VISION_BACKUP_ENABLED", "true")

    from src.core.config import Settings

    with pytest.raises(ValidationError, match="AI_SPAM_VISION_BACKUP_PROTOCOL"):
        Settings(_env_file=None)


@pytest.mark.unit
def test_config_typesafe_text_with_explicit_vision_protocol_starts(monkeypatch):
    """文本 Jev + Vision 显式指定 openai_chat → 正常启动。"""
    _captcha_env(monkeypatch, webapp_url=None, signature_key="")
    monkeypatch.setenv("AI_SPAM_PROTOCOL", "typesafe_systemone")
    monkeypatch.setenv("AI_SPAM_VISION_ENABLED", "true")
    monkeypatch.setenv("AI_SPAM_VISION_PROTOCOL", "openai_chat")

    from src.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.vision_protocol_effective == "openai_chat"
