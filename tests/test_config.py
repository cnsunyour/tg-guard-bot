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
