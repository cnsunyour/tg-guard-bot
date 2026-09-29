"""自定义验证脚本服务：三重审查编排 + revision 激活管理。

激活门槛三件套（任一不过不入库/不可激活）：
1. 静态风险审查（src/services/script_review.py，含行号报错）
2. AI 代码审查（fail-closed：审查不可用即阻断，绝不降级通过）
3. 沙盒 dry-run（ask + 一轮 verify，验证协议合法与判定可用）

dry-run 语义如实定位：可用性检查，非安全认证——能挡语法错/无解题，
不证明脚本对所有输入都正确。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from sandbox.protocol import AskResult
from src.ml.ai_contracts import CODE_REVIEW_RESULT_SCHEMA
from src.models.custom_verification import CustomVerificationRevision
from src.repositories.custom_verification_repo import CustomVerificationRepository
from src.services.sandbox_client import (
    SandboxClient,
    SandboxLanguage,
    SandboxProtocolError,
    SandboxUnavailableError,
    get_sandbox_client,
)
from src.services.script_review import ScriptViolation, review_source

# AI 审查 system prompt（PR4 起 i18n 化；当前面向上传命令的管理员反馈）
AI_REVIEW_SYSTEM_PROMPT = (
    "你是代码安全审查员。审查一段 Telegram 群组验证脚本（在强沙盒内运行，"
    "仅有语言标准库子集，无网络/文件/进程能力）。判断脚本是否包含高危行为：\n"
    "1. 尝试访问文件系统、网络、进程、环境变量\n"
    "2. 混淆变形（编码拼接、动态 getattr/exec、字符串构造模块名）\n"
    "3. 沙盒逃逸意图（反射内省、对象图遍历、ctypes/FFI）\n"
    "4. 恶意逻辑（对特定用户放行/针对特定用户判错的歧视性判定、误导性题面）\n"
    "普通验证逻辑（算术题、选择题、文本匹配）即使写法笨拙也不是风险。\n"
    "只输出一个 JSON 对象，字段严格为以下两个（不要增删字段、不要输出 "
    "JSON 之外的任何文字）：\n"
    '{"risk": "safe" 或 "risky", "reasons": ["判定理由1", "判定理由2"]}\n'
    "risk 为 risky 时 reasons 必须给出具体依据；为 safe 时 reasons 可给简短说明。"
)

# dry-run 使用的假用户上下文（不触碰任何真实成员）
_DRY_RUN_USER: dict[str, Any] = {
    "id": 0,
    "first_name": "DryRun",
    "username": None,
    "language_code": "zh-Hans",
}


@dataclass(slots=True)
class ReviewOutcome:
    """三重审查的最终结论：ok=True 时 revision 已入库。"""

    ok: bool
    stage: str  # 失败阶段: static / ai_unavailable / ai_risky / dry_run / done
    violations: list[str] = field(default_factory=list)
    revision: CustomVerificationRevision | None = None


class CustomVerificationService:
    """面向管理命令的自定义验证脚本服务。"""

    def __init__(self, sandbox: SandboxClient | None = None) -> None:
        self._sandbox = sandbox

    def _get_sandbox(self) -> SandboxClient:
        if self._sandbox is None:
            self._sandbox = get_sandbox_client()
        return self._sandbox

    async def review_and_save(
        self, group_id: int, language: str, source: str, uploaded_by: int
    ) -> ReviewOutcome:
        """三重审查全部通过则入库新 revision；任一步失败返回结构化结论。"""
        if language not in ("python", "javascript"):
            # 语言合法性前置校验 + 类型窄化（SandboxClient 契约为 Literal）
            return ReviewOutcome(ok=False, stage="static", violations=[f"不支持的语言: {language}"])
        sandbox_language: SandboxLanguage = "python" if language == "python" else "javascript"
        # 1. 静态风险审查
        violations: list[ScriptViolation] = review_source(language, source)
        if violations:
            return ReviewOutcome(
                ok=False,
                stage="static",
                violations=[f"行 {v.line}: {v.message}" for v in violations],
            )

        # 2. AI 代码审查（fail-closed：不可用/异常都阻断，绝不降级通过）
        ai_verdict = await self._ai_review(language, source)
        if ai_verdict is None:
            return ReviewOutcome(
                ok=False,
                stage="ai_unavailable",
                violations=["AI 审查不可用（未配置/超时/异常），按 fail-closed 阻断入库"],
            )
        if ai_verdict["risk"] != "safe":
            return ReviewOutcome(
                ok=False,
                stage="ai_risky",
                violations=[f"AI 审查判定高危: {r}" for r in ai_verdict["reasons"]],
            )

        # 3. 沙盒 dry-run（ask + 一轮 verify）
        dry_run = await self._dry_run(sandbox_language, source)
        if not dry_run["passed"]:
            return ReviewOutcome(
                ok=False,
                stage="dry_run",
                violations=[str(msg) for msg in dry_run["errors"]],
            )

        # 三关全过 → 入库（审查结论持久化，激活时校验）
        revision = CustomVerificationRevision(
            group_id=group_id,
            language=language,
            source=source,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            api_version=1,
            uploaded_by=uploaded_by,
            review_result={
                "static": {"passed": True, "violations": []},
                "ai": ai_verdict,
                "dry_run": dry_run,
            },
        )
        saved = await CustomVerificationRepository.create_revision(revision)
        logger.info(
            f"✅ 自定义验证脚本已入库 [group:{group_id}] [revision:{saved.id}] "
            f"[language:{language}] [source_bytes:{len(source.encode('utf-8'))}]"
        )
        return ReviewOutcome(ok=True, stage="done", revision=saved)

    async def _ai_review(self, language: str, source: str) -> dict[str, Any] | None:
        """AI 审查：返回 {'risk': ..., 'reasons': [...]}；不可用/异常返回 None。"""
        from src.ml.ai_detector import get_ai_detector

        try:
            result = await get_ai_detector().review_code(
                f"脚本语言: {language}\n\n```{language}\n{source}\n```",
                system_prompt=AI_REVIEW_SYSTEM_PROMPT,
                result_schema=CODE_REVIEW_RESULT_SCHEMA,
            )
        except Exception as exc:
            logger.warning(f"自定义脚本 AI 审查失败（阻断入库）: {exc}")
            return None
        reasons = result.get("reasons") if isinstance(result, dict) else None
        # 严格校验：缺字段/类型漂移（如 reasons 为数字）都视为审查失效 → 阻断。
        # 审查结论会持久化为激活门槛，宁可不入库也不放过畸形响应
        if (
            not isinstance(result, dict)
            or result.get("risk") not in ("safe", "risky")
            or not isinstance(reasons, list)
            or not all(isinstance(r, str) for r in reasons)
        ):
            logger.warning(f"AI 审查返回结构不合规，按阻断处理: {result!r:.200}")
            return None
        return {"risk": result["risk"], "reasons": reasons}

    async def _dry_run(self, sandbox_language: SandboxLanguage, source: str) -> dict[str, Any]:
        """沙盒试跑：ask 必须产出合法题目，verify 必须给出合法判定。

        只验证协议可用性（answer 传什么值都能过协议校验即算通过），
        不要求判对——判对与否是管理员业务，激活前由管理员 test 自查。
        """
        errors: list[str] = []
        challenge_id = f"dryrun-{uuid.uuid4().hex[:12]}"
        ask_ctx: dict[str, Any] = {
            "api_version": 1,
            "challenge_id": challenge_id,
            "group_id": 0,
            "user": dict(_DRY_RUN_USER),
            "locale": "zh-Hans",
            "issued_at": 0,
            "expires_at": 120,
            "attempt_no": 1,
            "state": None,
        }
        try:
            ask_result = await self._get_sandbox().execute_ask(
                sandbox_language, source, ask_ctx, timeout_ms=2000
            )
        except (SandboxUnavailableError, SandboxProtocolError) as exc:
            return {"passed": False, "errors": [f"ask 试跑失败: {exc}"]}

        if not isinstance(ask_result, AskResult) or not ask_result.text.strip():
            return {"passed": False, "errors": ["ask 未返回有效题面"]}

        # verify 试跑只要求协议合法 + 按钮模式至少一个选项可通过：
        # - 「无解题」检测（所有选项都 retry = 必挂新人）是 dry-run 的核心目标之一；
        #   单次试跑只能证明协议合法，拦不住 value/decision 键位写反的脚本
        # - 文本模式无法枚举正确答案，只能验协议合法性（判对与否靠管理员 test 自查）
        if ask_result.options:
            decisions: list[str] = []
            for option in ask_result.options:
                verify_ctx = dict(ask_ctx, state=ask_result.state, input=option.value)
                try:
                    verify_result = await self._get_sandbox().execute_verify(
                        sandbox_language, source, verify_ctx, timeout_ms=2000
                    )
                except (SandboxUnavailableError, SandboxProtocolError) as exc:
                    return {"passed": False, "errors": [f"verify 试跑失败: {exc}"]}
                decisions.append(verify_result.decision)
            if not any(decision == "pass" for decision in decisions):
                errors.append("所有按钮选项的 verify 判定均为 retry（无解题：任何答案都无法通过）")
        else:
            verify_ctx = dict(ask_ctx, state=ask_result.state, input="dry-run 样本答案")
            try:
                verify_result = await self._get_sandbox().execute_verify(
                    sandbox_language, source, verify_ctx, timeout_ms=2000
                )
            except (SandboxUnavailableError, SandboxProtocolError) as exc:
                return {"passed": False, "errors": [f"verify 试跑失败: {exc}"]}
            _ = verify_result  # 文本模式：协议合法即通过（合法值由类型层保证）

        return {
            "passed": not errors,
            "errors": errors,
            "sample_text": ask_result.text[:200],
        }

    async def activate(self, group_id: int, revision_id: int) -> bool:
        """激活 revision（三重审查结论齐全且通过才可；乐观并发防静默覆盖）。"""
        revision = await CustomVerificationRepository.get_group_revision(group_id, revision_id)
        if revision is None:
            logger.warning(
                f"激活失败: revision 不属于该群 [group:{group_id}] [revision:{revision_id}]"
            )
            return False
        if not self._review_passed(revision):
            logger.warning(f"激活失败: 审查结论不完整 [group:{group_id}] [revision:{revision_id}]")
            return False
        from src.repositories.group_repo import GroupRepository

        group = await GroupRepository.get_or_create(group_id)
        current = group.active_revision_id if group else None
        return await CustomVerificationRepository.activate_revision(group_id, revision_id, current)

    async def disable(self, group_id: int) -> bool:
        """停用脚本验证：只切开关，revision 指针保留（重新启用恢复同一版本）。

        回落群组原有 verification_type 的判定由读取路径完成：
        custom_verify_enabled 且 active_revision_id 非 NULL 才走脚本验证。
        """
        return await CustomVerificationRepository.set_custom_verify_enabled(group_id, False)

    async def get_active_revision(self, group_id: int) -> CustomVerificationRevision | None:
        return await CustomVerificationRepository.get_active_revision(group_id)

    @staticmethod
    def _review_passed(revision: CustomVerificationRevision) -> bool:
        review = revision.review_result
        if not isinstance(review, dict):
            return False
        static_ok = (
            isinstance(review.get("static"), dict) and review["static"].get("passed") is True
        )
        ai = review.get("ai")
        ai_ok = isinstance(ai, dict) and ai.get("risk") == "safe"
        dry = review.get("dry_run")
        dry_ok = isinstance(dry, dict) and dry.get("passed") is True
        return static_ok and ai_ok and dry_ok


_custom_verification_service: CustomVerificationService | None = None


def get_custom_verification_service() -> CustomVerificationService:
    """模块级单例（对齐 get_cas_service 模式）。"""
    global _custom_verification_service
    if _custom_verification_service is None:
        _custom_verification_service = CustomVerificationService()
    return _custom_verification_service
