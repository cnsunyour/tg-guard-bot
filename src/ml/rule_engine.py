"""规则引擎模块 - Stage 1 快速过滤

使用高级正则规则引擎替代原有的简单关键词匹配，实现：
- 多关键词联合检测（前瞻断言）
- Unicode 混淆检测（繁简体/同义词）
- 置信度分级（CRITICAL: 0.95, HIGH: 0.88, MEDIUM: 0.80, LOW: 0.70）
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, TypedDict
from urllib.parse import urlparse

from loguru import logger

from src.core.config import settings


class AnalysisResult(TypedDict):
    """规则引擎分析结果类型"""

    is_spam: bool
    confidence: float
    reasons: list[str]
    details: dict[str, Any]


class SpamRiskLevel(Enum):
    """垃圾风险等级"""

    CRITICAL = "critical"  # 🔴 极高：违法内容 0.95
    HIGH = "high"  # 🟠 高：加密货币诈骗 0.88
    MEDIUM = "medium"  # 🟡 中：陪聊服务 0.80
    LOW = "low"  # 🟢 低：普通广告 0.70


@dataclass(frozen=True)
class SpamRule:
    """垃圾检测规则定义"""

    id: str  # 规则唯一标识
    pattern: str  # 正则表达式模式
    risk_level: SpamRiskLevel  # 风险等级
    category: str  # 分类标签
    description: str  # 规则描述
    max_match_length: int = 200  # 最大匹配长度（性能优化）
    enabled: bool = True  # 是否启用

    @property
    def confidence(self) -> float:
        """根据风险等级返回置信度"""
        return {
            SpamRiskLevel.CRITICAL: 0.95,
            SpamRiskLevel.HIGH: 0.88,
            SpamRiskLevel.MEDIUM: 0.80,
            SpamRiskLevel.LOW: 0.70,
        }[self.risk_level]


class RegexRuleEngine:
    """高级正则规则引擎

    特性：
    - 预编译所有正则表达式（启动时）
    - 支持从 JSON 文件加载规则
    - 多关键词联合检测（前瞻断言）
    - 置信度分级（根据风险等级）
    """

    # 默认规则集（从用户提供的 10 条规则转换）
    DEFAULT_RULES: ClassVar[list[dict]] = [
        # 🔴 极高危险等级
        {
            "id": "social_engine_db",
            "pattern": r"免费社工库",
            "risk_level": "critical",
            "category": "illegal",
            "description": "社工库非法获取",
            "enabled": True,
        },
        {
            "id": "underage_content",
            "pattern": r"[呦幼][呦幼女童]",
            "risk_level": "critical",
            "category": "illegal",
            "description": "未成年人色情",
            "enabled": True,
        },
        {
            "id": "illegal_drugs",
            "pattern": r"(催情|迷)(药|💊)[\s\S]{0,50}听话水",
            "risk_level": "critical",
            "category": "illegal",
            "description": "违禁药品",
            "enabled": True,
            "max_match_length": 100,
        },
        # 🟠 高危险等级 - 加密货币诈骗
        {
            "id": "crypto_multi_keyword",
            "pattern": (
                r"(?i)(?=(.*(BTC|ETH|USDT)|以太|大[饼餅]|[幣币]圈|[现現][货货]|行情))"
                r"(?=.*(分析|咨[询詢]|跟[單单]|[進进][群裙]|私聊|助理|合[約约]|[觀观][點点]|[參参]考|"
                r"盈.{0,5}[虧亏损損]|策略|交流)).{1,200}"
            ),
            "risk_level": "high",
            "category": "crypto_scam",
            "description": "加密货币多关键词联合检测",
            "enabled": True,
            "max_match_length": 200,
        },
        {
            "id": "crypto_exchange_group",
            "pattern": (
                r"(以太|大[饼餅]|策略|[幣币]圈|交流|[现現][货货]|合[约約]|[觀观][點点]|"
                r"[參参]考|分析|咨[询詢]).{0,5}([群裙]|[频頻]道)"
            ),
            "risk_level": "high",
            "category": "crypto_scam",
            "description": "加密货币群组/频道",
            "enabled": True,
        },
        {
            "id": "unblock_quick",
            "pattern": r"(?=(.*解封))(?=.*秒结).{1,100}",
            "risk_level": "high",
            "category": "scam",
            "description": "解封秒结诈骗",
            "enabled": True,
        },
        {
            "id": "wealth_password",
            "pattern": r"([财財致]富.{0,5}密[码碼])",
            "risk_level": "high",
            "category": "scam",
            "description": "财富密码诈骗",
            "enabled": True,
        },
        # 🟡 中危险等级
        {
            "id": "private_chat_service",
            "pattern": r"(?=(.*私聊))(?=.*(发泄|欲望)).{1,100}",
            "risk_level": "medium",
            "category": "adult",
            "description": "私聊陪聊服务",
            "enabled": True,
        },
        {
            "id": "adult_chat_service",
            "pattern": r"(?=(.*[哥叔]))(?=.*(陪聊|聊聊|聊天|想[要约陪聊])[了的吗么])",
            "risk_level": "medium",
            "category": "adult",
            "description": "成人陪聊服务",
            "enabled": True,
        },
        {
            "id": "private_contact_service",
            "pattern": (
                r"([售卖][Uu]|[搞赚挣][钱米]|跑分|代收|[頭头]像|[简簡]介|不影响|"
                r"(相互|互相)(安慰|慰藉)|[资資]源|愿意的|懂的)"
                r"[\s\S]*([私找看睇点點][我莪]|[来莱俫])"
            ),
            "risk_level": "medium",
            "category": "adult",
            "description": "私密联系方式服务",
            "enabled": True,
        },
        # 🟢 低危险等级
        {
            "id": "trading_signals",
            "pattern": (
                r"(方向|[進进][場场]|[區区][间间])[\s\S]*止[盈损]|"
                r"[Kk]線[\s\S]*([說説]明|特徵|形成)|"
                r"(力|金|势)[\s\S]*营销"
            ),
            "risk_level": "low",
            "category": "trading",
            "description": "交易信号/营销",
            "enabled": True,
        },
        {
            "id": "suspicious_platforms",
            "pattern": r"亿万国际|交易宝|绿色棋游",
            "risk_level": "low",
            "category": "gambling",
            "description": "可疑博彩平台",
            "enabled": True,
        },
        {
            "id": "telegram_spam",
            "pattern": r"(?i)(tg|telegram)[\s\S]*(私发|代发|僵尸)",
            "risk_level": "low",
            "category": "spam",
            "description": "Telegram 垃圾推广",
            "enabled": True,
        },
    ]

    def __init__(
        self,
        rules: list[SpamRule] | None = None,
        config_path: str | None = None,
    ):
        """初始化正则规则引擎

        Args:
            rules: 自定义规则列表（优先级高于配置文件）
            config_path: 规则配置文件路径（JSON 格式）
        """
        self._compile_cache: dict[str, re.Pattern] = {}

        # 加载规则（优先使用传入的规则）
        if rules:
            self.rules = rules
        elif config_path:
            self.rules = self._load_rules_from_file(config_path)
        else:
            self.rules = self._load_default_rules()

        # 预编译所有规则
        self._precompile_all()

    def check(self, text: str) -> tuple[bool, SpamRule | None, str | None]:
        """执行正则规则检测

        Args:
            text: 待检测文本

        Returns:
            (是否命中, 命中的规则, 匹配到的文本片段)
        """
        # 性能优化：先截断文本
        text = text[:500]

        for rule in self.rules:
            if not rule.enabled:
                continue

            pattern = self._get_compiled_pattern(rule)

            try:
                match = pattern.search(text)
                if match:
                    matched_text = match.group(0)[: rule.max_match_length]
                    logger.debug(
                        f"正则规则命中: {rule.id} ({rule.description}), "
                        f"置信度: {rule.confidence}, 匹配: {matched_text}"
                    )
                    return True, rule, matched_text
            except re.error as e:
                logger.warning(f"规则 {rule.id} 正则错误: {e}")

        return False, None, None

    def _load_default_rules(self) -> list[SpamRule]:
        """加载默认规则集"""
        return [self._dict_to_rule(rule_dict) for rule_dict in self.DEFAULT_RULES]

    def _load_rules_from_file(self, config_path: str) -> list[SpamRule]:
        """从 JSON 文件加载规则

        Args:
            config_path: 配置文件路径

        Returns:
            规则列表
        """
        try:
            path = Path(config_path)
            if not path.exists():
                logger.warning(f"规则配置文件不存在: {config_path}，使用默认规则")
                return self._load_default_rules()

            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            rules = [self._dict_to_rule(rule_dict) for rule_dict in data.get("rules", [])]
            logger.info(f"从 {config_path} 加载了 {len(rules)} 条规则")
            return rules

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"加载规则配置文件失败: {e}，使用默认规则")
            return self._load_default_rules()

    @staticmethod
    def _dict_to_rule(rule_dict: dict) -> SpamRule:
        """将字典转换为 SpamRule 对象"""
        return SpamRule(
            id=rule_dict["id"],
            pattern=rule_dict["pattern"],
            risk_level=SpamRiskLevel(rule_dict["risk_level"]),
            category=rule_dict["category"],
            description=rule_dict["description"],
            max_match_length=rule_dict.get("max_match_length", 200),
            enabled=rule_dict.get("enabled", True),
        )

    def _precompile_all(self) -> None:
        """预编译所有规则的正则表达式"""
        for rule in self.rules:
            try:
                self._get_compiled_pattern(rule)
            except re.error as e:
                logger.error(f"规则 {rule.id} 编译失败: {e}")

    def _get_compiled_pattern(self, rule: SpamRule) -> re.Pattern:
        """获取编译后的正则表达式（带缓存）"""
        if rule.id not in self._compile_cache:
            self._compile_cache[rule.id] = re.compile(rule.pattern)
        return self._compile_cache[rule.id]


class RuleEngine:
    """规则引擎 - 基于规则的快速垃圾检测

    两层检测架构:
    1. 正则规则引擎 (替代原有简单关键词) - RegexRuleEngine
    2. 其他特征检测 (URL/联系方式等)
    """

    # Telegram 邀请链接模式
    TG_INVITE_PATTERNS: ClassVar[list[str]] = [
        r"t\.me/\+",
        r"t\.me/joinchat/",
        r"telegram\.me/\+",
        r"telegram\.me/joinchat/",
    ]

    # 可疑域名模式
    SUSPICIOUS_DOMAINS: ClassVar[list[str]] = [
        ".tk",
        ".ml",
        ".ga",
        ".cf",
        ".gq",  # 免费域名
    ]

    def __init__(
        self,
        whitelist_domains: list[str] | None = None,
        custom_rules: list[SpamRule] | None = None,
        regex_rules_config_path: str | None = None,
    ):
        """初始化规则引擎

        Args:
            whitelist_domains: 白名单域名列表
            custom_rules: 自定义正则规则（从配置文件加载）
            regex_rules_config_path: 正则规则配置文件路径
        """
        self.whitelist_domains = whitelist_domains or []

        # 正则规则引擎（替代原有的简单关键词匹配）
        self.regex_engine = RegexRuleEngine(
            rules=custom_rules,
            config_path=regex_rules_config_path,
        )

    def analyze(self, text: str) -> AnalysisResult:
        """综合分析文本 - 重构版本"""
        result: AnalysisResult = {
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }

        # 处理空值
        if not text:
            return result

        # 正则规则检测（替代原有的 check_keywords）
        is_match, rule, matched_text = self.regex_engine.check(text)
        if is_match and rule:
            result["confidence"] = rule.confidence
            result["reasons"].append(f"规则匹配: {rule.description}")
            result["details"].update(
                {
                    "rule_id": rule.id,
                    "category": rule.category,
                    "risk_level": rule.risk_level.value,
                    "matched_text": matched_text,
                }
            )

            # 🔴 极高危险等级直接返回，跳过后续检测
            if rule.risk_level == SpamRiskLevel.CRITICAL:
                result["is_spam"] = True
                return result

        # 原有检测逻辑：URL + 联系方式 + 其他特征

        # 检查 URL
        has_suspicious_url, urls, url_reason = self.check_urls(text)
        if has_suspicious_url:
            result["confidence"] = max(result["confidence"], 0.8)
            result["reasons"].append(url_reason)
            result["details"]["urls"] = urls

        # 检查联系方式
        has_contact, contact_type = self.check_contact_info(text)
        if has_contact:
            result["confidence"] = max(result["confidence"], 0.8)
            result["reasons"].append(f"包含联系方式: {contact_type}")

        # 检查重复字符
        if self.check_repeated_chars(text):
            result["confidence"] = max(result["confidence"], 0.7)
            result["reasons"].append("重复字符刷屏")

        # 检查频道提及
        if self.check_channel_mention(text):
            result["confidence"] = max(result["confidence"], 0.6)
            result["reasons"].append("包含频道提及")

        # 检查 Emoji 刷屏
        if self.check_emoji_flood(text):
            result["confidence"] = max(result["confidence"], 0.65)
            result["reasons"].append("Emoji 刷屏")

        if result["confidence"] >= settings.spam_threshold_rule:
            result["is_spam"] = True

        return result

    def check_urls(self, text: str) -> tuple[bool, list[str], str]:
        """检查 URL 和链接

        Returns:
            (是否可疑, URL列表, 原因)
        """
        # 提取所有 URL
        url_pattern = (
            r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
        )
        urls = re.findall(url_pattern, text)

        if not urls:
            return False, [], ""

        # 检查 Telegram 邀请链接
        for pattern in self.TG_INVITE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                logger.debug("检测到 Telegram 邀请链接")
                return True, urls, "Telegram 邀请链接"

        # 检查可疑域名
        for url in urls:
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.lower()

                # 检查白名单
                if any(d in domain for d in self.whitelist_domains):
                    continue

                # 检查可疑域名
                for suspicious in self.SUSPICIOUS_DOMAINS:
                    if domain.endswith(suspicious):
                        logger.debug(f"检测到可疑域名: {domain}")
                        return True, urls, f"可疑域名: {suspicious}"

            except Exception as e:
                logger.warning(f"解析 URL 失败: {url}, 错误: {e}")

        # 检查短链接（通常用于隐藏真实地址）
        short_link_domains = ["bit.ly", "tinyurl.com", "goo.gl", "t.cn", "suo.im"]
        for url in urls:
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.lower()
                if any(d in domain for d in short_link_domains):
                    logger.debug(f"检测到短链接: {domain}")
                    return True, urls, "短链接"
            except Exception as e:
                logger.debug(f"解析短链接失败（非关键）: {url}, 错误: {e}")

        return False, urls, ""

    def check_repeated_chars(
        self, text: str, length_threshold: int = 20, ratio_threshold: float = 0.7
    ) -> bool:
        """检查重复字符（如：哈哈哈哈）

        Args:
            length_threshold: 字符串长度阈值（低于此长度直接返回 False）
            ratio_threshold: 单个字符连续重复长度占比阈值（默认 0.7，即 70%）

        Returns:
            如果字符串长度超过阈值且单个字符的连续重复长度占比达到或超过阈值，返回 True

        Examples:
            "哈哈哈哈哈哈哈哈" (8个哈): 8/8=100% ≥ 0.7 → True
            "哈哈哈哈 好好好好" (4个哈+4个好): 4/8=50% < 0.7 → False
        """
        # 字符串长度不足，直接返回 False
        if len(text) < length_threshold:
            return False

        # 找到最长的单个字符连续重复次数
        max_repeated_count = 0
        current_count = 1

        for i in range(1, len(text)):
            if text[i] == text[i - 1]:
                current_count += 1
            else:
                max_repeated_count = max(max_repeated_count, current_count)
                current_count = 1

        # 处理最后一组重复字符
        max_repeated_count = max(max_repeated_count, current_count)

        # 计算最长单个字符连续重复长度占比
        ratio = max_repeated_count / len(text)
        if ratio >= ratio_threshold:
            logger.debug(
                f"检测到重复字符刷屏: {ratio:.2%} "
                f"(单个字符最长连续重复 {max_repeated_count}/{len(text)})"
            )
            return True
        return False

    def check_contact_info(self, text: str) -> tuple[bool, str]:
        """检查联系方式（微信、QQ、电话等）

        Returns:
            (是否包含, 类型)
        """
        # 微信号模式
        wechat_patterns = [
            r"微信[:：\s]*[a-zA-Z0-9_-]{5,20}",
            r"V信[:：\s]*[a-zA-Z0-9_-]{5,20}",
            r"wx[:：\s]*[a-zA-Z0-9_-]{5,20}",
        ]
        for pattern in wechat_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                logger.debug("检测到微信号")
                return True, "微信号"

        # QQ 号模式
        qq_pattern = r"QQ[:：\s]*[0-9]{5,12}"
        if re.search(qq_pattern, text, re.IGNORECASE):
            logger.debug("检测到 QQ 号")
            return True, "QQ号"

        # 电话号码模式（中国大陆）：限定前后无数字，避免误匹配长数字串中的片段
        phone_pattern = r"(?<!\d)1[3-9]\d{9}(?!\d)"
        if re.search(phone_pattern, text):
            logger.debug("检测到电话号码")
            return True, "电话号码"

        return False, ""

    def check_channel_mention(self, text: str) -> bool:
        """检查频道提及（@channel）"""
        if re.search(r"@[a-zA-Z0-9_]{5,}", text):
            logger.debug("检测到频道提及")
            return True
        return False

    def check_emoji_flood(self, text: str, threshold: float = 0.5) -> bool:
        """检查 Emoji 刷屏

        Args:
            threshold: Emoji 占比阈值（0-1）
        """
        if len(text) < 10:
            return False

        emoji_pattern = re.compile(
            "["
            "\U0001f600-\U0001f64f"  # 表情符号
            "\U0001f300-\U0001f5ff"  # 符号和象形文字
            "\U0001f680-\U0001f6ff"  # 交通和地图符号
            "\U0001f1e0-\U0001f1ff"  # 旗帜
            "]+",
            flags=re.UNICODE,
        )

        emoji_count = len(emoji_pattern.findall(text))
        total_chars = len(text)

        if emoji_count / total_chars > threshold:
            logger.debug(f"检测到 Emoji 刷屏: {emoji_count}/{total_chars}")
            return True

        return False


# 全局规则引擎实例
_rule_engine: RuleEngine | None = None


def get_rule_engine() -> RuleEngine:
    """获取全局规则引擎实例"""
    global _rule_engine
    if _rule_engine is None:
        _rule_engine = RuleEngine()
    return _rule_engine
