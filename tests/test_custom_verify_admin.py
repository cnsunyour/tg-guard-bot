"""自定义验证脚本管理命令的 handler 测试（mock service/权限，无真实网络）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message
from pydantic import ValidationError

from sandbox.protocol import AskResult, OptionButton
from src.bot.handlers import custom_verify as handler
from src.core.config import settings
from src.services.custom_verification import get_custom_verification_service
from src.services.sandbox_client import VerifyResult

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CHAT = -100123
ADMIN = 42
STRANGER = 7


def _message(chat_type: str = "group", user_id: int = ADMIN) -> Message:
    message = AsyncMock(spec=Message)
    message.chat = SimpleNamespace(id=CHAT, type=chat_type)
    message.from_user = SimpleNamespace(id=user_id)
    message.sender_chat = None  # 匿名管理员识别字段（message 版权限检查访问）
    message.answer = AsyncMock()
    return message


@pytest.fixture
def admin_user(monkeypatch: pytest.MonkeyPatch):
    """调用者即超级管理员（settings.admin_ids 直通）。"""
    monkeypatch.setattr(settings, "admin_ids", {ADMIN})
    return ADMIN


@pytest.fixture
def feature_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "custom_verification_enabled", True)
    client = SimpleNamespace(configured=True)
    monkeypatch.setattr("src.services.sandbox_client.get_sandbox_client", lambda: client)


class TestGlobalSwitch:
    async def test_globally_disabled_blocks_all(self, admin_user, monkeypatch):
        monkeypatch.setattr(settings, "custom_verification_enabled", False)
        message = _message()
        localizer = SimpleNamespace(t=lambda key, **kw: key)
        await handler.cmd_customverify(
            message,
            AsyncMock(),
            localizer,
            SimpleNamespace(args="upload"),
        )
        # 全局关闭时任何子命令都提示未启用，不触达上传流程
        message.answer.assert_awaited_once_with("customverify.globally_disabled.message")

    async def test_stranger_denied_in_group(self, admin_user, monkeypatch):
        # 非管理员（admin_ids 不含 + 权限查询 False）：权限拒绝
        monkeypatch.setattr(handler, "check_admin_permission_by_id", AsyncMock(return_value=False))
        message = _message(user_id=STRANGER)
        localizer = SimpleNamespace(t=lambda key, **kw: key)
        await handler.cmd_customverify(
            message, AsyncMock(), localizer, SimpleNamespace(args="status")
        )
        message.answer.assert_awaited_once_with("customverify.permission_denied.message")


class TestUploadFlow:
    async def test_upload_writes_waiting_key(self, admin_user, feature_on, monkeypatch):
        redis = SimpleNamespace(
            set=AsyncMock(return_value=True), getdel=AsyncMock(return_value=None)
        )
        monkeypatch.setattr(handler, "get_redis", lambda: redis)
        monkeypatch.setattr(handler, "check_admin_permission_by_id", AsyncMock(return_value=True))
        message = _message()
        localizer = SimpleNamespace(t=lambda key, **kw: key)

        await handler._handle_upload_request(message, AsyncMock(), localizer, CHAT, ADMIN)

        redis.set.assert_awaited_once()
        args = redis.set.await_args
        assert args.args[0] == f"custom_verify_upload:{ADMIN}"
        assert args.args[1] == str(CHAT)
        # 群内提示 + 主动私聊推送（各一条）
        assert message.answer.await_count == 1
        assert message.answer.await_args.args[0] == "customverify.upload.prompt.message"

    async def test_private_document_without_waiting_key_ignored(
        self, admin_user, feature_on, monkeypatch
    ):
        # 非上传流程的私聊文档：静默忽略（不 answer、不下载）
        redis = SimpleNamespace(getdel=AsyncMock(return_value=None))
        monkeypatch.setattr(handler, "get_redis", lambda: redis)
        message = _message(chat_type="private")
        message.document = SimpleNamespace(file_name="x.py", file_size=10)

        await handler.on_custom_verify_document(message, AsyncMock())

        message.answer.assert_not_awaited()


class TestJevAIReviewGuard:
    """Jev 协议不支持脚本 AI 审查：经 Vision 通道执行，双保险（启动期 + 运行时）。"""

    def _base_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
        monkeypatch.setenv("ADMIN_IDS", "[123456789]")
        monkeypatch.setenv("DB_PASSWORD", "test_password")
        monkeypatch.setenv("REDIS_PASSWORD", "redis_password")
        monkeypatch.setenv("MODEL_SIGNATURE_KEY", "a" * 64)

    def test_config_jev_primary_without_vision_rejected(self, monkeypatch: pytest.MonkeyPatch):
        """Jev 主协议 + 无可用 Vision 通道 → 启动期 ValueError（审查无处可去）。"""
        self._base_env(monkeypatch)
        monkeypatch.setenv("CUSTOM_VERIFICATION_ENABLED", "true")
        monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8080")
        monkeypatch.setenv("SANDBOX_API_KEY", "k" * 32)
        monkeypatch.setenv("AI_SPAM_PROTOCOL", "typesafe_systemone")
        # 显式关 Vision（测试进程会读本地 .env 的 Vision 配置，须隔离）
        monkeypatch.setenv("AI_SPAM_VISION_ENABLED", "false")
        monkeypatch.setenv("AI_SPAM_VISION_BACKUP_ENABLED", "false")

        from src.core.config import Settings

        with pytest.raises(ValidationError, match="Vision"):
            Settings()

    def test_config_jev_primary_with_vision_passes(self, monkeypatch: pytest.MonkeyPatch):
        """Jev 主协议 + Vision 通道配齐 → 启动通过（审查自动经 Vision 通道）。"""
        self._base_env(monkeypatch)
        monkeypatch.setenv("CUSTOM_VERIFICATION_ENABLED", "true")
        monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8080")
        monkeypatch.setenv("SANDBOX_API_KEY", "k" * 32)
        monkeypatch.setenv("AI_SPAM_PROTOCOL", "typesafe_systemone")
        monkeypatch.setenv("AI_SPAM_VISION_ENABLED", "true")
        monkeypatch.setenv("AI_SPAM_VISION_PROTOCOL", "openai_chat")
        monkeypatch.setenv("AI_SPAM_VISION_API_KEY", "vk")
        monkeypatch.setenv("AI_SPAM_VISION_API_BASE", "https://vision.example/v1")
        monkeypatch.setenv("AI_SPAM_VISION_MODEL", "gpt-4o-mini")

        from src.core.config import Settings

        settings = Settings()  # 不抛即通过
        assert settings.custom_verification_enabled is True

    def test_config_jev_backup_protocol_allowed(self, monkeypatch: pytest.MonkeyPatch):
        """Jev 作 backup 协议不拦（审查固定走主 provider，反垃圾主备不受限）。"""
        self._base_env(monkeypatch)
        monkeypatch.setenv("CUSTOM_VERIFICATION_ENABLED", "true")
        monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8080")
        monkeypatch.setenv("SANDBOX_API_KEY", "k" * 32)
        monkeypatch.setenv("AI_SPAM_PROTOCOL", "openai_chat")
        monkeypatch.setenv("AI_SPAM_BACKUP_PROTOCOL", "typesafe_systemone")
        monkeypatch.setenv("AI_SPAM_BACKUP_API_KEY", "bk")
        monkeypatch.setenv("AI_SPAM_BACKUP_MODEL", "jev-latest")

        from src.core.config import Settings

        settings = Settings()  # 不抛即通过
        assert settings.ai_spam_backup_protocol == "typesafe_systemone"

    async def test_review_code_routes_to_vision_channel(self, monkeypatch: pytest.MonkeyPatch):
        """Jev 主协议：审查自动经 Vision 通道（primary 的语义无关请求不发出）。"""
        from src.ml.ai_contracts import CODE_REVIEW_RESULT_SCHEMA
        from src.ml.ai_detector import HybridAIDetector
        from src.ml.ai_protocols import AIProtocol

        detector = HybridAIDetector()
        detector.primary = SimpleNamespace(
            is_available=True,
            name="primary",
            config=SimpleNamespace(protocol=AIProtocol.TYPESAFE_SYSTEMONE),
            _call_api=AsyncMock(),
        )
        vision_call = AsyncMock(return_value={"risk": "safe", "reasons": []})
        detector.vision_primary = SimpleNamespace(
            is_available=True, name="vision_primary", _call_api=vision_call
        )
        detector.vision_backup = SimpleNamespace(is_available=False)

        result = await detector.review_code(
            "def ask(ctx): ...",
            system_prompt="review",
            result_schema=CODE_REVIEW_RESULT_SCHEMA,
        )
        assert result == {"risk": "safe", "reasons": []}
        detector.primary._call_api.assert_not_awaited()
        vision_call.assert_awaited_once_with(
            "def ask(ctx): ...",
            system_prompt="review",
            result_schema=CODE_REVIEW_RESULT_SCHEMA,
        )

    async def test_review_code_jev_without_vision_raises(self, monkeypatch: pytest.MonkeyPatch):
        """Jev 主协议 + Vision 主备都不可用 → AIServiceError（fail-closed）。"""
        from src.ml.ai_contracts import CODE_REVIEW_RESULT_SCHEMA
        from src.ml.ai_detector import AIServiceError, HybridAIDetector
        from src.ml.ai_protocols import AIProtocol

        detector = HybridAIDetector()
        detector.primary = SimpleNamespace(
            is_available=True,
            name="primary",
            config=SimpleNamespace(protocol=AIProtocol.TYPESAFE_SYSTEMONE),
            _call_api=AsyncMock(),
        )
        detector.vision_primary = SimpleNamespace(is_available=False)
        detector.vision_backup = SimpleNamespace(is_available=False)

        with pytest.raises(AIServiceError, match="Vision"):
            await detector.review_code(
                "def ask(ctx): ...",
                system_prompt="review",
                result_schema=CODE_REVIEW_RESULT_SCHEMA,
            )
        detector.primary._call_api.assert_not_awaited()


class TestDryRunUnsolvableDetection:
    """dry-run 的无解题检测：按钮模式逐选项试跑，全部 retry 拒绝入库。"""

    def _service_with_sandbox(self, monkeypatch: pytest.MonkeyPatch, verify_by_input):
        """构造 mock 沙盒并种入 service 单例（返回 service 供断言）。"""
        ask_result = AskResult(
            text="这个群主要讨论什么？",
            options=[
                OptionButton(text="技术交流", value="tech"),
                OptionButton(text="发广告", value="ad"),
            ],
            state={"correct": "tech"},
        )
        client = SimpleNamespace(
            execute_ask=AsyncMock(return_value=ask_result),
            execute_verify=AsyncMock(
                side_effect=lambda _lang, _src, ctx, timeout_ms=2000: VerifyResult(
                    decision=verify_by_input(ctx["input"])
                )
            ),
        )
        # 直接种单例的 _sandbox：custom_verification.py 顶部已绑定
        # get_sandbox_client 名字，patch 模块属性对已建单例无效
        service = get_custom_verification_service()
        monkeypatch.setattr(service, "_sandbox", client)
        return service

    async def test_all_options_retry_rejected(self, monkeypatch: pytest.MonkeyPatch):
        """value/decision 键位写反的脚本：所有选项都 retry → dry-run 拒绝。"""
        service = self._service_with_sandbox(monkeypatch, lambda _input: "retry")
        dry_run = await service._dry_run("python", "SRC")
        assert dry_run["passed"] is False
        assert any("无解题" in str(e) for e in dry_run["errors"])

    async def test_solvable_script_passes(self, monkeypatch: pytest.MonkeyPatch):
        """正常脚本：至少一个选项能 pass → dry-run 通过。"""
        service = self._service_with_sandbox(
            monkeypatch, lambda inp: "pass" if inp == "tech" else "retry"
        )
        dry_run = await service._dry_run("python", "SRC")
        assert dry_run["passed"] is True
