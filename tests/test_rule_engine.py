"""规则引擎测试"""

import pytest


@pytest.mark.unit
def test_regex_rule_engine():
    """测试正则规则引擎"""
    from src.ml.rule_engine import RegexRuleEngine, SpamRiskLevel, SpamRule

    # 测试加密货币诈骗规则
    crypto_rule = SpamRule(
        id="test_crypto",
        pattern=r"(?=(.*BTC))(?=.*(私聊|跟单)).{1,100}",
        risk_level=SpamRiskLevel.HIGH,
        category="crypto",
        description="测试规则",
    )
    engine = RegexRuleEngine([crypto_rule])

    # 应该命中（包含 BTC 和私聊）
    is_match, rule, _ = engine.check("BTC行情分析，私聊带单")
    assert is_match
    assert rule.confidence == 0.88

    # 不应该命中（只有 BTC，没有带单）
    is_match, _, _ = engine.check("BTC价格今天涨了")
    assert not is_match

    # 测试极高危险等级规则
    critical_rule = SpamRule(
        id="test_critical",
        pattern=r"免费社工库",
        risk_level=SpamRiskLevel.CRITICAL,
        category="illegal",
        description="测试极高危险规则",
    )
    engine_critical = RegexRuleEngine([critical_rule])

    is_match, rule, _ = engine_critical.check("这里有免费社工库，加微信获取")
    assert is_match
    assert rule.confidence == 0.95


@pytest.mark.unit
def test_rule_engine_with_regex_rules():
    """测试规则引擎集成正则规则"""
    from src.ml.rule_engine import RuleEngine

    # 使用默认正则规则
    engine = RuleEngine()

    # 测试加密货币诈骗（应该被正则规则捕获）
    result = engine.analyze("BTC行情分析，私聊带单，日赚十万")
    assert result["is_spam"]
    assert result["confidence"] >= 0.88
    # 现在 reasons 是编码格式 "rule_match:rule_id=..."（英文稳定 id，文案走 catalog）
    assert result["reasons"][0].startswith("rule_match:rule_id=")
    assert "crypto_multi_keyword" in result["reasons"][0]
    assert result["details"]["category"] == "crypto_scam"

    # 测试违禁药品（应该被 CRITICAL 规则捕获）
    result = engine.analyze("催情药听话水，欲购从速")
    assert result["is_spam"]
    assert result["confidence"] == 0.95
    assert result["details"]["risk_level"] == "critical"


@pytest.mark.unit
def test_rule_engine_url_detection():
    """测试 URL 检测（原有逻辑保持不变）"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 可疑链接
    spam_urls = [
        "点击 http://bit.ly/xyz123 领取",
        "访问 http://t.cn/abcdef 了解更多",
        "https://promo.tk/free",
    ]

    for text in spam_urls:
        result = engine.analyze(text)
        assert result["confidence"] > 0.3, f"应该检测到可疑链接: {text}"


@pytest.mark.unit
def test_rule_engine_contact_info_detection():
    """测试联系方式检测（原有逻辑保持不变）"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 包含联系方式
    contact_texts = [
        "加我微信：wx123456",
        "QQ：123456789",
        "手机号：13800138000",
    ]

    for text in contact_texts:
        result = engine.analyze(text)
        assert result["confidence"] > 0.4, f"应该检测到联系方式: {text}"

    # 长数字串不应被识别为手机号（前/后有数字）
    non_phone_texts = [
        "订单号138001380001234",  # 后面跟数字
        "流水号01380013800012",  # 前面有数字
        "6225881234567890",  # 银行卡号片段
    ]
    for text in non_phone_texts:
        has_contact, contact_type = engine.check_contact_info(text)
        assert not (has_contact and contact_type == "电话号码"), (
            f"不应将长数字串识别为电话号码: {text}"
        )


@pytest.mark.unit
def test_rule_engine_combined_score():
    """测试综合评分（正则规则 + 其他特征）"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 多个特征组合的垃圾消息
    spam_combined = "BTC行情分析私聊带单 点击链接 http://bit.ly/promo"
    result = engine.analyze(spam_combined)
    assert result["is_spam"]
    # 加密货币规则置信度 0.88，URL 置信度 0.8，应该取最大值
    assert result["confidence"] >= 0.88

    # 空文本
    assert engine.analyze("")["confidence"] == 0.0


@pytest.mark.unit
def test_rule_engine_unicode_detection():
    """测试 Unicode 混淆检测（繁简体/同义词）"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 测试繁简体混淆
    result = engine.analyze("大餅行情分析，私聊带单")
    assert result["is_spam"]
    assert result["details"]["category"] == "crypto_scam"

    # 测试同义词替换
    result = engine.analyze("搞钱私聊，想赚钱的来")
    assert result["is_spam"]
    assert result["details"]["category"] == "adult"


@pytest.mark.unit
def test_rule_engine_normal_messages():
    """测试正常消息不应该被误判"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 正常消息
    normal_texts = [
        "大家好，我是新来的",
        "今天天气不错",
        "有人知道怎么学Python吗？",
        "BTC价格今天涨了",  # 只有 BTC，没有其他关键词
    ]

    for text in normal_texts:
        result = engine.analyze(text)
        # 正常消息的置信度应该较低（可能有频道提及等轻微特征）
        assert result["confidence"] < 0.5, f"正常消息不应该被误判为垃圾: {text}"


@pytest.mark.unit
def test_rule_engine_critical_short_circuit():
    """测试极高危险规则短路退出"""
    from src.ml.rule_engine import RuleEngine

    engine = RuleEngine()

    # 极高危险规则应该直接返回，不继续检测其他特征
    result = engine.analyze("免费社工库，加微信abc，点击http://bit.ly/xyz")
    assert result["is_spam"]
    assert result["confidence"] == 0.95
    assert result["details"]["risk_level"] == "critical"
    # 应该只有一个原因（短路退出，不继续检测 URL）
    assert len(result["reasons"]) == 1


@pytest.mark.unit
def test_rule_engine_whitelist_domains():
    """测试白名单域名功能"""
    from src.ml.rule_engine import RuleEngine

    # 自定义白名单
    engine = RuleEngine(
        whitelist_domains=["github.com", "python.org"],
    )

    # 白名单域名不应触发 URL 检测
    text_with_whitelist = "查看 https://github.com/user/repo 了解更多"
    result = engine.analyze(text_with_whitelist)
    # 应该不会被 URL 检测到垃圾（但可能有其他特征）
    assert "可疑域名" not in " ".join(result["reasons"])


@pytest.mark.unit
def test_regex_rule_risk_levels():
    """测试不同风险等级的置信度"""
    from src.ml.rule_engine import SpamRiskLevel

    # 验证各风险等级的置信度
    assert SpamRiskLevel.CRITICAL.value == "critical"
    assert SpamRiskLevel.HIGH.value == "high"
    assert SpamRiskLevel.MEDIUM.value == "medium"
    assert SpamRiskLevel.LOW.value == "low"


@pytest.mark.unit
def test_prefilter_gate_blocks_regex():
    """测试关键词预筛门禁：未全组命中则跳过正则匹配"""
    from src.ml.rule_engine import RegexRuleEngine, SpamRiskLevel, SpamRule

    rule = SpamRule(
        id="test_prefilter",
        pattern=r"(?=.*[带帶])(?=.*缺[钱錢])(?=.*兄弟)(?=.*(打底|[进進]群)).{1,120}",
        risk_level=SpamRiskLevel.HIGH,
        category="scam",
        description="预筛测试",
        prefilter=(("带", "帶"), ("缺钱", "缺錢"), ("兄弟",), ("打底", "进群", "進群")),
    )
    engine = RegexRuleEngine([rule])

    # 全组命中 → 通过预筛并命中正则
    is_match, hit, _ = engine.check("带两个缺钱的兄弟，进群一起搞")
    assert is_match
    assert hit.id == "test_prefilter"

    # 缺少"缺钱"组 → 预筛拦截（即便其余特征齐全）
    is_match, _, _ = engine.check("带上兄弟一起进群打底")
    assert not is_match


@pytest.mark.unit
def test_prefilter_semantics_equivalent():
    """测试预筛是原正则的必要条件：命中正则的文本必然通过预筛（判定不变）"""
    from src.ml.rule_engine import RegexRuleEngine, SpamRiskLevel, SpamRule

    rule = SpamRule(
        id="test_eq",
        pattern=r"(?=.*[带帶])(?=.*缺[钱錢])(?=.*兄弟)(?=.*(打底|[进進]群)).{1,120}",
        risk_level=SpamRiskLevel.HIGH,
        category="scam",
        description="等价性测试",
        prefilter=(("带", "帶"), ("缺钱", "缺錢"), ("兄弟",), ("打底", "进群", "進群")),
    )
    engine = RegexRuleEngine([rule])

    # 真实垃圾模板变体（含简繁体、截断）均应命中
    spam_samples = [
        "🤍美美吖🎉 带两个缺钱的兄弟，一天保你一万打底，进群找王哥",
        "帶幾個缺錢的兄弟，進群一起搞",
        "带上缺钱的兄弟，打底也能赚",
    ]
    for s in spam_samples:
        is_match, _, _ = engine.check(s)
        assert is_match, f"应命中但未命中: {s}"

    # 正常文本不应误判
    normal_samples = [
        "我带两个朋友去吃饭",
        "这个游戏打底分挺高的，兄弟们进群一起玩",  # 缺"缺钱"
        "兄弟你缺钱吗，我借你点",  # 缺"带"+"打底/进群"
    ]
    for s in normal_samples:
        is_match, _, _ = engine.check(s)
        assert not is_match, f"应放行但误判: {s}"


@pytest.mark.unit
def test_prefilter_disabled_by_default():
    """测试未配置 prefilter 的规则行为不变（默认空，正常执行正则）"""
    from src.ml.rule_engine import RegexRuleEngine, SpamRiskLevel, SpamRule

    rule = SpamRule(
        id="test_no_prefilter",
        pattern=r"免费社工库",
        risk_level=SpamRiskLevel.CRITICAL,
        category="illegal",
        description="无预筛规则",
    )
    assert rule.prefilter == ()
    engine = RegexRuleEngine([rule])
    is_match, _, _ = engine.check("这里有免费社工库")
    assert is_match
