"""AI 检测协议共享的结构化输出契约。

业务层只维护一份 Text/Vision JSON Schema，三个协议 adapter 各自包装成
对应格式（OpenAI ``response_format.json_schema`` / Responses ``text.format`` /
Anthropic ``output_config.format.schema`` 或 tool ``input_schema``）。

Schema 使用三协议公共子集：所有字段 required、所有 object 设置
``additionalProperties: false``，不使用 ``minimum``/``maximum`` 等 Anthropic
不支持的约束（置信度范围由本地业务校验）。
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
