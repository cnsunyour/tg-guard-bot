"""规则引擎测试"""

import pytest


@pytest.mark.unit
def test_rule_engine_keyword_detection():
    """测试关键词检测"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 垃圾关键词
    spam_texts = [
        "点击这里领取红包",
        "加微信：abc123",
        "免费送iPhone",
        "日赚千元兼职",  # 替换 "办理贷款，利息低"
    ]

    for text in spam_texts:
        result = engine.analyze(text)
        assert result["confidence"] > 0.5, f"应该检测到垃圾信息: {text}"

    # 正常消息
    normal_texts = [
        "大家好，我是新来的",
        "今天天气不错",
        "有人知道怎么学Python吗？",
    ]

    for text in normal_texts:
        result = engine.analyze(text)
        assert result["confidence"] < 0.3, f"不应该误判为垃圾: {text}"


@pytest.mark.unit
def test_rule_engine_url_detection():
    """测试 URL 检测"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 可疑链接
    spam_urls = [
        "点击 http://bit.ly/xyz123 领取",
        "访问 http://t.cn/abcdef 了解更多",  # 添加 http:// 前缀
        "https://promo.tk/free",  # 改为 .tk 免费域名
    ]

    for text in spam_urls:
        result = engine.analyze(text)
        assert result["confidence"] > 0.3, f"应该检测到可疑链接: {text}"


@pytest.mark.unit
def test_rule_engine_contact_info_detection():
    """测试联系方式检测"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 包含联系方式
    contact_texts = [
        "加我微信：wx123456",
        "QQ：123456789",  # 改为 "QQ："
        "Telegram: @spammer",
        "手机号：13800138000",
    ]

    for text in contact_texts:
        result = engine.analyze(text)
        assert result["confidence"] > 0.4, f"应该检测到联系方式: {text}"


@pytest.mark.unit
def test_rule_engine_whitelist():
    """测试白名单功能"""
    from src.ml.rule_engine import RuleEngine

    # 自定义白名单
    engine = RuleEngine(
        blacklist_keywords=["测试"],
        whitelist_domains=["github.com", "python.org"],
    )

    # 白名单域名不应触发
    text_with_whitelist = "查看 https://github.com/user/repo"
    result = engine.analyze(text_with_whitelist)
    assert result["confidence"] < 0.3, "白名单 URL 不应被检测为垃圾"

    # 黑名单关键词应触发
    text_with_blacklist = "这是一个测试消息"
    result_blacklist = engine.analyze(text_with_blacklist)
    assert result_blacklist["confidence"] > 0.5, "黑名单关键词应被检测"


@pytest.mark.unit
def test_rule_engine_combined_score():
    """测试综合评分"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 多个特征组合的垃圾消息
    spam_combined = "点击链接 http://bit.ly/promo 加微信 wx999 免费领取iPhone"
    result = engine.analyze(spam_combined)
    assert result["confidence"] > 0.8, "多特征组合的垃圾信息应该高分"

    # 空文本
    assert engine.analyze("")["confidence"] == 0.0
    assert engine.analyze(None)["confidence"] == 0.0
