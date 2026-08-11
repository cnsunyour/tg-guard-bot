"""AI 检测 HTTP 协议适配层。

将 OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 三种
协议的差异（端点、认证、请求体、响应结构、结构化输出承载方式）收敛到
adapter，业务层（``ai_detector.py``）只持有中立配置和统一结果类型。

核心抽象
--------
- :class:`ProtocolAdapter`：协议无关的请求构造 + 响应解析接口
- :class:`ProtocolResponse`：统一响应（终止状态 + 结构化结果）
- :class:`ResponseTerminatedError`：模型正常返回但无可用结果（refusal /
  token 截断等），不重试同一 provider，由协调器直接走备份

结构化输出策略（``structured_output_mode``）
-------------------------------------------
- ``strict``：发 schema 强约束（OpenAI json_schema / Anthropic output_config 或 tool）
- ``legacy``：不发 schema，靠 prompt 约束 + JSON 兜底解析（兼容第三方接口）
- ``auto``：由 :func:`create_protocol_adapter` 按官方 base / 模型能力解析为 strict 或 legacy

Anthropic 承载方式（``anthropic_output_mode``）
----------------------------------------------
- ``native``：``output_config.format``（Claude 4.5+ 原生结构化输出，GA 无需 beta header）
- ``tool``：tool_use + tool_choice 强制（全模型兼容）
- ``auto``：由 factory 按模型 allowlist 选择
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast
from urllib.parse import urlsplit, urlunsplit

from src.ml.ai_contracts import JSONSchema


class AIProtocol(StrEnum):
    """支持的 AI HTTP 协议。"""

    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class StructuredOutputMode(StrEnum):
    """结构化输出兼容策略。AUTO 由 factory 解析为 STRICT 或 LEGACY。"""

    AUTO = "auto"
    STRICT = "strict"
    LEGACY = "legacy"


class AnthropicOutputMode(StrEnum):
    """Anthropic 结构化结果承载方式。AUTO 由 factory 解析为 NATIVE 或 TOOL。"""

    AUTO = "auto"
    NATIVE = "native"
    TOOL = "tool"


class ResponseTermination(StrEnum):
    """模型响应的统一终止状态。非 COMPLETED 均视为无可用结果。"""

    COMPLETED = "completed"
    REFUSAL = "refusal"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"


@dataclass(frozen=True, slots=True)
class ProtocolResponse:
    """协议层解析后的统一响应。

    COMPLETED 时 ``result`` 为结构化检测字典；其余终止状态 ``result`` 为 None、
    ``detail`` 携带协议侧原始原因供日志展示。
    """

    termination: ResponseTermination
    result: dict[str, Any] | None = None
    detail: str = ""


class ResponseTerminatedError(Exception):
    """模型返回了 HTTP 正常响应，但未产出可用检测结果。

    与网络/超时/格式等可重试错误区分：终止类错误不重试同一 provider，
    直接由协调器尝试备份，且不计入熔断器的连续失败统计。
    """

    def __init__(self, termination: ResponseTermination, detail: str = "") -> None:
        self.termination = termination
        self.detail = detail
        message = f"AI 响应提前终止: {termination.value}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


# Anthropic 原生结构化输出（output_config.format）支持的模型前缀。
# 未命中此列表的模型在 auto 模式下退化为 Tool Use。新模型发布时需补全。
_ANTHROPIC_NATIVE_MODEL_PREFIXES: Final[tuple[str, ...]] = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
)

# Anthropic Tool Use 模式下承载检测结果的工具名
_ANTHROPIC_TOOL_NAME: Final[str] = "report_spam_detection"


def _strip_markdown_fence(content: str) -> str:
    """剥离完整包裹响应的 Markdown 代码围栏（含 ```` ```json ```` 等带语言变体）。

    仅处理首尾都是围栏行的完整包裹；不处理片段性围栏。
    """
    stripped = content.strip()
    lines = stripped.splitlines()
    fence = chr(96) * 3  # ```
    if len(lines) < 3:
        return stripped
    if not lines[0].strip().startswith(fence) or lines[-1].strip() != fence:
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _decode_json_object(content: str, *, allow_fallback: bool) -> dict[str, Any]:
    """解析 JSON object。

    strict 路径（``allow_fallback=False``）只接受纯净 JSON，失败即报错——
    模型已受 schema 约束，任何偏差都属异常。legacy 路径兼容模型在 JSON 外
    附加说明文本或 Markdown 围栏的情况：先试围栏剥离，再用
    :class:`json.JSONDecoder` 的 ``raw_decode`` 定位第一个可解析的对象
    （比旧版非贪婪正则更健壮，能处理嵌套大括号）。
    """
    try:
        parsed: Any = json.loads(content)
    except json.JSONDecodeError as exact_error:
        if not allow_fallback:
            raise ValueError(f"无法解析严格结构化响应: {content[:200]}") from exact_error

        unfenced = _strip_markdown_fence(content)
        if unfenced != content.strip():
            try:
                parsed = json.loads(unfenced)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)

        decoder = json.JSONDecoder()
        for offset, character in enumerate(unfenced):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(unfenced, offset)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                return cast("dict[str, Any]", candidate)
        raise ValueError(f"无法解析 AI 响应为 JSON object: {content[:200]}") from exact_error

    if not isinstance(parsed, dict):
        raise ValueError(f"AI 响应不是 JSON object: {type(parsed).__name__}")
    return cast("dict[str, Any]", parsed)


def _completed_text(content: str, *, allow_fallback: bool) -> ProtocolResponse:
    """将模型返回的文本内容封装为 COMPLETED 响应。"""
    if not content:
        raise ValueError("API 响应内容为空")
    return ProtocolResponse(
        termination=ResponseTermination.COMPLETED,
        result=_decode_json_object(content, allow_fallback=allow_fallback),
    )


class ProtocolAdapter(ABC):
    """AI HTTP 协议差异的边界。

    所有方法接收中立参数（system/user/image/schema），输出协议特定结构。
    ``structured_output_mode`` 必须由 factory 解析后再传入（不接受 AUTO）。
    """

    def __init__(self, structured_output_mode: StructuredOutputMode) -> None:
        if structured_output_mode is StructuredOutputMode.AUTO:
            raise ValueError("adapter 必须接收已解析的 structured_output_mode（非 auto）")
        self.structured_output_mode = structured_output_mode

    @property
    def _allow_json_fallback(self) -> bool:
        """legacy 路径允许 JSON 兜底解析；strict 路径要求纯净 JSON。"""
        return self.structured_output_mode is StructuredOutputMode.LEGACY

    @abstractmethod
    def build_url(self, api_base: str) -> str:
        """构造协议 endpoint。"""
        raise NotImplementedError

    @abstractmethod
    def build_headers(self, api_key: str) -> dict[str, str]:
        """构造认证 header。"""
        raise NotImplementedError

    @abstractmethod
    def build_text_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        """构造文本检测请求体。"""
        raise NotImplementedError

    @abstractmethod
    def build_vision_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        image_b64: str,
        mime: str,
        detail: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        """构造 Vision 检测请求体。"""
        raise NotImplementedError

    @abstractmethod
    def parse_response(self, response_json: dict[str, Any]) -> ProtocolResponse:
        """解析协议响应，返回统一终止状态与结构化结果。"""
        raise NotImplementedError


class OpenAIChatAdapter(ProtocolAdapter):
    """OpenAI Chat Completions 协议。

    结构化输出：strict 发 ``response_format.json_schema``；legacy 文本不发
    ``response_format``、Vision 发 ``json_object``（保持与改造前行为一致，
    保护 DeepSeek/Moonshot/OpenRouter 等第三方兼容接口）。
    """

    def build_url(self, api_base: str) -> str:
        return f"{api_base.rstrip('/')}/chat/completions"

    def build_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _apply_format(self, payload: dict[str, Any], schema: JSONSchema, *, vision: bool) -> None:
        if self.structured_output_mode is StructuredOutputMode.STRICT:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "spam_detection_vision" if vision else "spam_detection_text",
                    "schema": schema,
                    "strict": True,
                },
            }
        elif vision:
            # legacy Vision 仍用 json_object（改造前行为）；文本 legacy 完全不发
            payload["response_format"] = {"type": "json_object"}

    def build_text_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        }
        self._apply_format(payload, schema, vision=False)
        return payload

    def build_vision_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        image_b64: str,
        mime: str,
        detail: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{image_b64}",
                                "detail": detail,
                            },
                        },
                    ],
                },
            ],
        }
        self._apply_format(payload, schema, vision=True)
        return payload

    def parse_response(self, response_json: dict[str, Any]) -> ProtocolResponse:
        choices = response_json.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("API 响应格式错误：缺少 choices")

        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("API 响应格式错误：choice 不是 object")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("API 响应格式错误：缺少 message")

        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            return ProtocolResponse(ResponseTermination.REFUSAL, detail=refusal)

        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            return ProtocolResponse(ResponseTermination.MAX_TOKENS, detail="length")
        if finish_reason == "content_filter":
            return ProtocolResponse(ResponseTermination.CONTENT_FILTER, detail="content_filter")
        if finish_reason not in (None, "stop"):
            raise ValueError(f"Chat Completions 非预期 finish_reason: {finish_reason}")

        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("API 响应内容不是字符串")
        return _completed_text(content, allow_fallback=self._allow_json_fallback)


class OpenAIResponsesAdapter(ProtocolAdapter):
    """OpenAI Responses API 协议。

    请求用 ``input``（非 ``messages``）+ ``text.format`` 承载 schema；
    Vision 用 ``input_image``（``image_url`` 为字符串、``detail`` 同级），
    与 Chat 的 ``image_url: {url, detail}`` 结构不同。响应需遍历
    ``output[]`` 找 ``type=message`` 的项（前面可能有 reasoning item），
    再在其 ``content`` 里找 ``output_text`` 或 ``refusal`` block。
    """

    def build_url(self, api_base: str) -> str:
        return f"{api_base.rstrip('/')}/responses"

    def build_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _apply_format(self, payload: dict[str, Any], schema: JSONSchema, *, vision: bool) -> None:
        if self.structured_output_mode is StructuredOutputMode.STRICT:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "spam_detection_vision" if vision else "spam_detection_text",
                    "schema": schema,
                    "strict": True,
                }
            }
        elif vision:
            payload["text"] = {"format": {"type": "json_object"}}

    def build_text_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "max_output_tokens": max_output_tokens,
        }
        self._apply_format(payload, schema, vision=False)
        return payload

    def build_vision_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        image_b64: str,
        mime: str,
        detail: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_text},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime};base64,{image_b64}",
                            "detail": detail,
                        },
                    ],
                },
            ],
            "max_output_tokens": max_output_tokens,
        }
        self._apply_format(payload, schema, vision=True)
        return payload

    def parse_response(self, response_json: dict[str, Any]) -> ProtocolResponse:
        status = response_json.get("status")
        if status == "incomplete":
            details = response_json.get("incomplete_details")
            reason = details.get("reason", "") if isinstance(details, dict) else ""
            termination = (
                ResponseTermination.CONTENT_FILTER
                if reason == "content_filter"
                else ResponseTermination.MAX_TOKENS
            )
            return ProtocolResponse(termination, detail=str(reason))
        if status != "completed":
            raise ValueError(f"Responses API 非预期 status: {status}")

        output = response_json.get("output")
        if not isinstance(output, list):
            raise ValueError("Responses API 响应格式错误：缺少 output")

        # 遍历找 message 项（前面可能有 reasoning 项），再在其 content 里找
        # output_text 或 refusal block
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content_blocks = item.get("content")
            if not isinstance(content_blocks, list):
                continue
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "refusal":
                    return ProtocolResponse(
                        ResponseTermination.REFUSAL,
                        detail=str(block.get("refusal", "")),
                    )
                if block_type == "output_text":
                    text = block.get("text")
                    if not isinstance(text, str):
                        raise ValueError("Responses output_text.text 不是字符串")
                    return _completed_text(text, allow_fallback=self._allow_json_fallback)

        raise ValueError("Responses API 响应中没有 output_text 或 refusal")


class AnthropicMessagesAdapter(ProtocolAdapter):
    """Anthropic Messages API 协议。

    认证用 ``x-api-key`` + ``anthropic-version``；``system`` 是顶级字段（不在
    messages）；``max_tokens`` 必填。结构化输出两种承载：

    - native：``output_config.format.schema``（Claude 4.5+，GA 无需 beta header）
    - tool：定义 tool（``input_schema``）+ ``tool_choice`` 强制，从 ``content[]``
      取 ``tool_use.input``

    legacy 模式不发任何结构化约束，退化为 prompt + JSON 兜底。
    Vision 用 ``source.base64``（无 ``detail`` 概念，传入的 detail 参数被忽略）。
    """

    def __init__(
        self,
        structured_output_mode: StructuredOutputMode,
        output_mode: AnthropicOutputMode,
    ) -> None:
        super().__init__(structured_output_mode)
        if output_mode is AnthropicOutputMode.AUTO:
            raise ValueError("adapter 必须接收已解析的 anthropic_output_mode（非 auto）")
        self.output_mode = output_mode

    def build_url(self, api_base: str) -> str:
        # 同时接受含/不含 /v1 的 base：path 末段是 v1 则直接追加 /messages，
        # 否则追加 /v1/messages（适配 https://api.anthropic.com 与
        # https://api.anthropic.com/v1 两种常见写法）
        parts = urlsplit(api_base.rstrip("/"))
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"Anthropic API Base 无效: {api_base}")
        path = parts.path.rstrip("/")
        path = f"{path}/messages" if path.split("/")[-1] == "v1" else f"{path}/v1/messages"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def build_headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _apply_format(self, payload: dict[str, Any], schema: JSONSchema) -> None:
        # legacy 优先：不发任何结构化约束，退化为 prompt + JSON 兜底
        if self.structured_output_mode is StructuredOutputMode.LEGACY:
            return
        if self.output_mode is AnthropicOutputMode.NATIVE:
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            }
            return
        # tool 模式：强制调用承载结果的工具，模型按 input_schema 填参数。
        # strict:True 启用 Strict Tool Use，保证 tool_use.input 严格符合 schema
        payload["tools"] = [
            {
                "name": _ANTHROPIC_TOOL_NAME,
                "description": "Return the spam detection result.",
                "input_schema": schema,
                "strict": True,
            }
        ]
        payload["tool_choice"] = {
            "type": "tool",
            "name": _ANTHROPIC_TOOL_NAME,
            "disable_parallel_tool_use": True,
        }

    def build_text_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_text}],
            "max_tokens": max_output_tokens,
        }
        self._apply_format(payload, schema)
        return payload

    def build_vision_payload(
        self,
        model: str,
        system_prompt: str,
        user_text: str,
        image_b64: str,
        mime: str,
        detail: str,
        schema: JSONSchema,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
            "max_tokens": max_output_tokens,
        }
        self._apply_format(payload, schema)
        return payload

    def parse_response(self, response_json: dict[str, Any]) -> ProtocolResponse:
        stop_reason = response_json.get("stop_reason")
        if stop_reason == "refusal":
            return ProtocolResponse(ResponseTermination.REFUSAL, detail="refusal")
        if stop_reason in ("max_tokens", "model_context_window_exceeded"):
            return ProtocolResponse(
                ResponseTermination.MAX_TOKENS,
                detail=str(stop_reason),
            )

        content = response_json.get("content")
        if not isinstance(content, list):
            raise ValueError("Anthropic 响应格式错误：缺少 content")

        use_tool = (
            self.structured_output_mode is not StructuredOutputMode.LEGACY
            and self.output_mode is AnthropicOutputMode.TOOL
        )
        if use_tool:
            if stop_reason != "tool_use":
                raise ValueError(f"Anthropic Tool Use 非预期 stop_reason: {stop_reason}")
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use" or block.get("name") != _ANTHROPIC_TOOL_NAME:
                    continue
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    raise ValueError("Anthropic tool_use.input 不是 object")
                return ProtocolResponse(
                    ResponseTermination.COMPLETED,
                    result=cast("dict[str, Any]", tool_input),
                )
            raise ValueError("Anthropic 响应缺少目标 tool_use block")

        if stop_reason != "end_turn":
            raise ValueError(f"Anthropic 非预期 stop_reason: {stop_reason}")
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Anthropic text block 内容不是字符串")
            return _completed_text(text, allow_fallback=self._allow_json_fallback)
        raise ValueError("Anthropic 响应缺少 text block")


def _supports_anthropic_native(model: str) -> bool:
    """模型是否在 Anthropic 原生结构化输出 allowlist 内。

    处理 OpenRouter 风格的 provider 前缀（如 ``anthropic/claude-sonnet-4-5``）。
    """
    normalized = model.lower().rsplit("/", 1)[-1]
    return normalized.startswith(_ANTHROPIC_NATIVE_MODEL_PREFIXES)


def create_protocol_adapter(
    *,
    protocol: str,
    model: str,
    structured_output_mode: str,
    anthropic_output_mode: str,
) -> ProtocolAdapter:
    """根据 provider 配置创建稳定的协议适配器。

    AUTO 在此解析为具体值：
    - ``structured_output_mode=auto``：openai_chat 降级 legacy（保护默认协议向后兼容，
      避免旧模型 gpt-3.5/gpt-4-turbo 升级后 400）；openai_responses 升级 strict；
      anthropic_messages 支持 native 的模型升级 strict，旧模型降级 legacy
    - ``anthropic_output_mode=auto``：模型在 native allowlist 用 native，否则 tool

    解析后的 adapter 是稳定的（配置不变则行为不变），provider 可长期持有。
    """
    try:
        protocol_value = AIProtocol(protocol)
        structured_value = StructuredOutputMode(structured_output_mode)
        anthropic_value = AnthropicOutputMode(anthropic_output_mode)
    except ValueError as error:
        raise ValueError(f"AI 协议配置无效: {error}") from error

    if structured_value is StructuredOutputMode.AUTO:
        if protocol_value is AIProtocol.OPENAI_CHAT:
            # 默认协议保守：auto → legacy，避免旧模型（gpt-3.5/gpt-4-turbo）升级后
            # 发 json_schema 触发 400。需要 schema 保证请显式 strict
            structured_value = StructuredOutputMode.LEGACY
        elif protocol_value is AIProtocol.OPENAI_RESPONSES:
            # Responses 是新协议，用户主动选用，默认 strict
            structured_value = StructuredOutputMode.STRICT
        else:  # ANTHROPIC_MESSAGES
            # 支持 native 的模型用 strict（native 或 strict tool 都保证 schema）；
            # 旧模型无严格 schema 能力，降为 legacy（prompt + JSON 兜底）
            structured_value = (
                StructuredOutputMode.STRICT
                if _supports_anthropic_native(model)
                else StructuredOutputMode.LEGACY
            )

    if protocol_value is AIProtocol.OPENAI_CHAT:
        return OpenAIChatAdapter(structured_value)
    if protocol_value is AIProtocol.OPENAI_RESPONSES:
        return OpenAIResponsesAdapter(structured_value)

    if anthropic_value is AnthropicOutputMode.AUTO:
        anthropic_value = (
            AnthropicOutputMode.NATIVE
            if _supports_anthropic_native(model)
            else AnthropicOutputMode.TOOL
        )
    return AnthropicMessagesAdapter(structured_value, anthropic_value)
