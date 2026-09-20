"""AI 检测协议共享的结构化输出契约。

业务层只维护一份 Text/Vision JSON Schema，三个 LLM 协议 adapter 各自包装成
对应格式（OpenAI ``response_format.json_schema`` / Responses ``text.format`` /
Anthropic ``output_config.format.schema`` 或 tool ``input_schema``）。

Schema 使用三协议公共子集：所有字段 required、所有 object 设置
``additionalProperties: false``，不使用 ``minimum``/``maximum`` 等 Anthropic
不支持的约束（置信度范围由本地业务校验）。

TypeSafe System One（Jev）不生成文本、不接受 JSON Schema，改由固定的
typed questions 约束输出，其契约同样集中在本模块（见 ``SYSTEMONE_*``）。
"""

from typing import Any, Final

# 业务层与 adapter 之间传递的 JSON Schema 类型别名
JSONSchema = dict[str, Any]

# 文本垃圾检测结果 schema（三协议共用）
TEXT_RESULT_SCHEMA: Final[JSONSchema] = {
    "type": "object",
    "properties": {
        "is_spam": {"type": "boolean", "description": "是否为垃圾信息"},
        "confidence": {"type": "number", "description": "是垃圾的置信度 0.0-1.0"},
        "reason": {"type": "string", "description": "简短判断理由"},
    },
    "required": ["is_spam", "confidence", "reason"],
    "additionalProperties": False,
}

# Vision 垃圾检测结果 schema（文本三项 + extracted_text）
VISION_RESULT_SCHEMA: Final[JSONSchema] = {
    "type": "object",
    "properties": {
        "is_spam": {"type": "boolean", "description": "是否为垃圾信息"},
        "confidence": {"type": "number", "description": "是垃圾的置信度 0.0-1.0"},
        "reason": {"type": "string", "description": "简短判断理由"},
        "extracted_text": {"type": "string", "description": "图片中提取的全部可读文字"},
    },
    "required": ["is_spam", "confidence", "reason", "extracted_text"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# TypeSafe System One（Jev）typed questions
# ---------------------------------------------------------------------------

# Jev 可返回的垃圾类别 code：既是 Choice 问题的选项键，也是 reason code
# ``ai_category:category=<code>`` 的参数值（展示层按 locale 渲染，见 antispam_render）
SYSTEMONE_CATEGORY_CODES: Final[tuple[str, ...]] = (
    "advertising",
    "gambling",
    "adult",
    "scam",
    "traffic",
    "normal",
)

# 文本检测固定两问：is_spam（Noul，是垃圾的概率）+ category（Choice，类别）。
# instructions / criteria 用中文：实测对中文群消息，中文判据的分离度优于英文。
# state 既可能是原文，也可能是 ContextService.format_context_for_ai 拼出的带
# 【对话回复链】【群组最近对话】【待检测消息】分节的字符串，故 instructions 明确
# 要求结合分节上下文判断。
SYSTEMONE_TEXT_QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    "is_spam": {
        "type": "noul",
        "instructions": (
            "判断 state 中的待检测消息是否为 Telegram 群垃圾信息"
            "（广告推广、赌博博彩、色情服务、诈骗欺诈、引流拉群等）。"
            "若 state 含【对话回复链】【群组最近对话】【待检测消息】等分节，"
            "只对【待检测消息】作判断，并结合其余分节判断它是否属于正常对话的一部分："
            "对他人提问的自然回应、与群内话题一致的分享应视为正常。"
        ),
        "criteria": {
            "true": (
                "推销商品或服务、博彩、成人服务、刷单/兼职日结/贷款/投资带单/USDT 回收等诈骗、"
                "引导加微信/QQ/公众号/其它群或下载 APP"
            ),
            "false": "日常聊天、技术讨论、提问、回答他人问题、正常分享链接或个人经历",
        },
    },
    "category": {
        "type": "choice",
        "instructions": "state 中的待检测消息属于哪一类？正常消息选 normal。",
        "criteria": {
            "advertising": "商品或服务推广（含 VPN、代开发票等）",
            "gambling": "博彩、赌场、下注、稳赚不赔",
            "adult": "约炮、上门、裸聊、成人服务",
            "scam": "刷单、兼职日结、贷款、投资带单、USDT 回收等诈骗",
            "traffic": "引流：加群、关注公众号、下载 APP、加微信/QQ",
            "normal": "正常消息：聊天、技术讨论、咨询、回答问题、正常分享",
        },
    },
}
