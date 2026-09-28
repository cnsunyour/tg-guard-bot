"""沙盒执行协议：请求/响应模型与严格 JSON 校验。

此模块是 bot 与沙盒容器共享的**协议单一来源**：主镜像与沙盒镜像都包含
本文件（各自 Dockerfile 单独 COPY），两侧以同一套模型解析 /execute 的
请求与结果。修改本文件的字段约束时，两侧无需各自同步。

信任边界说明：bot 是协议中唯一可信的一端。沙盒返回的任何内容都必须
经过本模块校验后才可使用（即使沙盒容器被攻破，返回的恶意结构也止步于
pydantic 模型），因此 bot 侧对结果做与沙盒侧相同的强校验。
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 协议版本：bot 与沙盒镜像升级不同步时以此协商，不匹配按服务不可用处理
PROTOCOL_VERSION = 1

# ---- 资源与内容上限（两侧一致，改动需同步评估 render_text_image 等下游）----

# 脚本源码上限（UTF-8 字节）
MAX_SOURCE_BYTES = 64 * 1024
# 执行结果 stdout 上限（字节）。留出题面（≤2000 码点，CJK 最坏 ~6KB）+
# 按钮 + state（≤4KB）与 JSON 序列化膨胀的余量
MAX_OUTPUT_BYTES = 32 * 1024
# stderr 采集上限（仅用于故障详情，不进协议结果）
MAX_STDERR_BYTES = 16 * 1024
# HTTP 请求体上限（字节）
MAX_REQUEST_BODY_BYTES = 256 * 1024
# 脚本私有状态上限（紧凑 JSON 字节；存 bot 侧 Redis，verify 时回传）
MAX_STATE_BYTES = 4 * 1024
# 题面文本上限（Unicode 码点；文字题将图片化渲染）
MAX_QUESTION_CODEPOINTS = 2000
# 按钮数量上限
MAX_OPTIONS = 8
# 按钮文本上限（码点）
MAX_OPTION_TEXT_CODEPOINTS = 64
# 执行 wall-clock 超时请求范围（毫秒；wall-clock = timeout_ms + 启动余量）
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 5000
# 脚本结果 JSON 的最大嵌套深度（防恶意深嵌套放大下游递归开销）
MAX_JSON_DEPTH = 8


def validate_json_value(value: Any, *, max_depth: int = MAX_JSON_DEPTH) -> None:
    """递归校验值可表示为严格 JSON：类型白名单 + 深度限制 + 拒绝非有限浮点。

    pydantic 的 JSON 解析不限制嵌套深度；恶意构造的深嵌套结构会在下游
    序列化/递归处理时放大开销，故在协议边界显式检查。深度超限或类型非法
    直接抛 ValueError，由调用方转为协议错误。
    """

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            raise ValueError(f"JSON 嵌套深度超过 {max_depth}")
        if node is None or isinstance(node, (str, int, bool)):
            return
        if isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError("JSON 不允许 NaN/Infinity")
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ValueError("JSON 对象键必须为字符串")
                _walk(item, depth + 1)
            return
        raise ValueError(f"JSON 类型不允许: {type(node).__name__}")

    _walk(value, 0)


def compact_json_bytes(value: Any) -> bytes:
    """紧凑序列化（无空格分隔符），用于字节数统计与落盘。"""

    import json

    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


class OptionButton(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    """ask 返回的单个按钮：text 给用户看，value 是服务端保存的逻辑值。

    value 不进 callback_data（Telegram 64 字节硬限制放不下），只按索引
    存入服务端映射；字符集限制便于日志展示与未来扩展。
    """

    text: str = Field(min_length=1, max_length=MAX_OPTION_TEXT_CODEPOINTS)
    value: str = Field(min_length=1, max_length=64)

    @field_validator("value")
    @classmethod
    def _check_value_charset(cls, v: str) -> str:
        # pydantic 的 pattern 约束是 re.match（前缀匹配）语义，"bad value!" 会
        # 因前缀 "bad" 而通过——必须显式 fullmatch
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v) is None:
            raise ValueError("value 只允许字母/数字/下划线/连字符")
        return v

    @field_validator("text", "value")
    @classmethod
    def _reject_control_chars(cls, v: str) -> str:
        # 按钮文本/值一律拒绝控制字符（含 \t\r）；isprintable 为 False 的
        # 字符会破坏日志与协议输出可读性，按钮场景无合法用途
        if any(not ch.isprintable() for ch in v):
            raise ValueError("不允许控制字符")
        return v


class AskResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    """ask 入口的合法返回：题面 + 可选按钮 + 私有状态。

    state 在 ask 之后不可变，verify 只读；不下发 Telegram、不进 callback_data。
    options 为空列表表示文本作答模式（用户在私聊回复任意文本）。
    """

    text: str = Field(min_length=1, max_length=MAX_QUESTION_CODEPOINTS)
    options: list[OptionButton] = Field(default_factory=list, max_length=MAX_OPTIONS)
    state: Any = None

    @field_validator("text")
    @classmethod
    def _reject_control_chars(cls, v: str) -> str:
        # 题面允许多行（\n），其余控制字符（含空字节/\t\r）拒绝
        if any(not ch.isprintable() and ch != "\n" for ch in v):
            raise ValueError("题面含非法控制字符")
        return v

    @field_validator("state")
    @classmethod
    def _validate_state(cls, v: Any) -> Any:
        if v is None:
            return v
        validate_json_value(v)
        if len(compact_json_bytes(v)) > MAX_STATE_BYTES:
            raise ValueError(f"state 超过 {MAX_STATE_BYTES} 字节")
        return v


class VerifyResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    """verify 入口的合法返回：只有通过/答错两种判定。

    刻意不提供 ban/kick 等处罚字段——处罚由群配置与现有验证系统决定，
    脚本只描述业务判定（见设计文档威胁模型一节）。
    """

    decision: Literal["pass", "retry"]


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    """bot → 沙盒的执行请求。"""

    protocol_version: int
    language: Literal["python", "javascript"]
    source: str
    entry: Literal["ask", "verify"]
    context: dict[str, Any]
    timeout_ms: int = Field(ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS)

    @field_validator("protocol_version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != PROTOCOL_VERSION:
            raise ValueError(f"协议版本不支持: {v}")
        return v

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("脚本源码为空")
        if len(v.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError(f"脚本源码超过 {MAX_SOURCE_BYTES} 字节")
        return v

    @field_validator("context")
    @classmethod
    def _validate_context(cls, v: dict[str, Any]) -> dict[str, Any]:
        # ctx 由 bot 构造（可信），仍做防御性校验以约束协议整体形状
        validate_json_value(v)
        return v


class ExecuteResponse(BaseModel):
    """沙盒 → bot 的执行响应。

    ok=True 时 result 为对应入口的强类型结果（ask→AskResult / verify→VerifyResult，
    由服务端在 /execute 内解析后填充）；ok=False 时 error 为机器可读分类、
    detail 为供管理员/日志排查的简短说明（可能含脚本 stderr 片段）。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    ok: bool
    result: AskResult | VerifyResult | None = None
    error: (
        Literal["timeout", "memory", "crash", "bad_output", "busy", "bad_input", "unsupported"]
        | None
    ) = None
    detail: str = Field(default="", max_length=4096)

    @field_validator("detail")
    @classmethod
    def _strip_control(cls, v: str) -> str:
        # 脚本 stderr 可能含任意控制字符，进入日志/管理界面统一清洗
        return "".join(ch for ch in v if ch == "\n" or ch.isprintable())
