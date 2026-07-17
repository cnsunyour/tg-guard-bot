"""Pytest 配置和共享 fixtures"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """设置测试环境变量"""
    # 设置测试环境标志
    os.environ["TESTING"] = "true"
    os.environ["LOG_LEVEL"] = "DEBUG"


@pytest.fixture
def mock_bot_token():
    """模拟 Bot Token"""
    return "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_test_token"


@pytest.fixture
def mock_admin_id():
    """模拟管理员 ID"""
    return 123456789


@pytest.fixture
def mock_settings(monkeypatch, mock_bot_token, mock_admin_id):
    """模拟完整配置"""
    monkeypatch.setenv("BOT_TOKEN", mock_bot_token)
    monkeypatch.setenv("ADMIN_IDS", str(mock_admin_id))
    monkeypatch.setenv("DB_PASSWORD", "test_password_12345")
    monkeypatch.setenv("REDIS_PASSWORD", "redis_test_password_12345")
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "a" * 64)

    from src.core.config import Settings

    return Settings()


@pytest.fixture
def sample_spam_texts():
    """垃圾消息样本"""
    return [
        "点击链接免费领取iPhone",
        "加微信：wx123456 办理贷款",
        "访问 http://bit.ly/promo 了解更多",
        "🎁免费送礼物🎁加QQ群：123456789",
        "Telegram: @spam_bot 推荐股票",
    ]


@pytest.fixture
def sample_normal_texts():
    """正常消息样本"""
    return [
        "大家好，我是新来的",
        "今天天气不错，适合出门",
        "有人知道怎么学Python吗？",
        "谢谢大家的帮助",
        "周末有什么计划？",
    ]
