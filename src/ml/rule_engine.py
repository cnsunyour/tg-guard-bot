"""规则引擎模块 - Stage 1 快速过滤"""

import re
from typing import Optional, List, Tuple
from urllib.parse import urlparse
from loguru import logger

from src.core.config import settings


class RuleEngine:
    """规则引擎 - 基于规则的快速垃圾检测"""

    # 默认关键词黑名单（可通过配置扩展）
    DEFAULT_BLACKLIST_KEYWORDS = [
        # 常见广告词
        "加微信", "加V信", "加wx", "私聊我", "扫码", "点击链接",
        # 赌博相关
        "赌博", "下注", "返水", "充值", "提款", "博彩",
        # 色情相关
        "约炮", "一夜情", "美女", "小姐姐上门",
        # 诈骗相关
        "刷单", "兼职", "日赚", "躺赚", "零投资", "高回报",
        # 垃圾推广
        "点赞", "关注", "转发", "免费送", "领取",
    ]

    # Telegram 邀请链接模式
    TG_INVITE_PATTERNS = [
        r"t\.me/\+",
        r"t\.me/joinchat/",
        r"telegram\.me/\+",
        r"telegram\.me/joinchat/",
    ]

    # 可疑域名模式
    SUSPICIOUS_DOMAINS = [
        ".tk", ".ml", ".ga", ".cf", ".gq",  # 免费域名
    ]

    def __init__(
        self,
        blacklist_keywords: Optional[List[str]] = None,
        whitelist_domains: Optional[List[str]] = None,
    ):
        """初始化规则引擎

        Args:
            blacklist_keywords: 自定义黑名单关键词
            whitelist_domains: 白名单域名列表
        """
        self.blacklist_keywords = blacklist_keywords or self.DEFAULT_BLACKLIST_KEYWORDS
        self.whitelist_domains = whitelist_domains or []

    def check_keywords(self, text: str) -> Tuple[bool, Optional[str]]:
        """检查关键词黑名单

        Returns:
            (是否匹配, 匹配的关键词)
        """
        text_lower = text.lower()
        for keyword in self.blacklist_keywords:
            if keyword.lower() in text_lower:
                logger.debug(f"关键词黑名单命中: {keyword}")
                return True, keyword
        return False, None

    def check_urls(self, text: str) -> Tuple[bool, List[str], str]:
        """检查 URL 和链接

        Returns:
            (是否可疑, URL列表, 原因)
        """
        # 提取所有 URL
        url_pattern = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
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
                # ✅ L6: 添加日志，不静默吞掉异常
                logger.debug(f"解析短链接失败（非关键）: {url}, 错误: {e}")

        return False, urls, ""

    def check_repeated_chars(self, text: str, threshold: int = 5) -> bool:
        """检查重复字符（如：啊啊啊啊啊）

        Args:
            threshold: 重复次数阈值
        """
        pattern = rf"(.)\1{{{threshold},}}"
        if re.search(pattern, text):
            logger.debug("检测到重复字符")
            return True
        return False

    def check_contact_info(self, text: str) -> Tuple[bool, str]:
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

        # 电话号码模式（中国大陆）
        phone_pattern = r"1[3-9]\d{9}"
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
            "\U0001F600-\U0001F64F"  # 表情符号
            "\U0001F300-\U0001F5FF"  # 符号和象形文字
            "\U0001F680-\U0001F6FF"  # 交通和地图符号
            "\U0001F1E0-\U0001F1FF"  # 旗帜
            "]+",
            flags=re.UNICODE,
        )

        emoji_count = len(emoji_pattern.findall(text))
        total_chars = len(text)

        if emoji_count / total_chars > threshold:
            logger.debug(f"检测到 Emoji 刷屏: {emoji_count}/{total_chars}")
            return True

        return False

    def analyze(self, text: str) -> dict:
        """综合分析文本

        Returns:
            分析结果字典，包含所有检测项
        """
        result = {
            "is_spam": False,
            "confidence": 0.0,
            "reasons": [],
            "details": {},
        }

        # 检查关键词
        is_blacklist, keyword = self.check_keywords(text)
        if is_blacklist:
            result["is_spam"] = True
            result["confidence"] = 0.9
            result["reasons"].append(f"关键词黑名单: {keyword}")

        # 检查 URL
        has_suspicious_url, urls, url_reason = self.check_urls(text)
        if has_suspicious_url:
            result["is_spam"] = True
            result["confidence"] = max(result["confidence"], 0.85)
            result["reasons"].append(url_reason)
            result["details"]["urls"] = urls

        # 检查联系方式
        has_contact, contact_type = self.check_contact_info(text)
        if has_contact:
            result["is_spam"] = True
            result["confidence"] = max(result["confidence"], 0.8)
            result["reasons"].append(f"包含联系方式: {contact_type}")

        # 检查重复字符
        if self.check_repeated_chars(text):
            result["is_spam"] = True
            result["confidence"] = max(result["confidence"], 0.7)
            result["reasons"].append("重复字符刷屏")

        # 检查频道提及
        if self.check_channel_mention(text):
            result["confidence"] = max(result["confidence"], 0.6)
            result["reasons"].append("包含频道提及")

        # 检查 Emoji 刷屏
        if self.check_emoji_flood(text):
            result["is_spam"] = True
            result["confidence"] = max(result["confidence"], 0.65)
            result["reasons"].append("Emoji 刷屏")

        return result


# 全局规则引擎实例
_rule_engine: Optional[RuleEngine] = None


def get_rule_engine() -> RuleEngine:
    """获取全局规则引擎实例"""
    global _rule_engine
    if _rule_engine is None:
        _rule_engine = RuleEngine()
    return _rule_engine
