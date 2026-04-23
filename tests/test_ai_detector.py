"""AI 检测器单元测试

测试主备引擎的 client 重建、熔断、切换逻辑
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ml.ai_detector import (
    AIServiceConfig,
    AIServiceError,
    BackupAIServiceProvider,
    HybridAIDetector,
    PrimaryAIServiceProvider,
    _is_vision_model,
    _read_image_as_base64,
)


@pytest.fixture
def mock_client():
    """模拟 httpx.AsyncClient"""
    client = MagicMock()
    client.aclose = AsyncMock()
    client.post = AsyncMock()
    return client


@pytest.fixture
def mock_config():
    """模拟 AI 服务商配置"""
    return AIServiceConfig(
        enabled=True,
        api_key="test-key",
        api_base="https://api.test.com/v1",
        model="test-model",
        timeout=10,
        max_retries=2,
    )


@pytest.fixture
def primary_provider():
    """主服务商 fixture"""
    provider = PrimaryAIServiceProvider()
    # Mock _create_client 避免实际创建 httpx client
    with patch.object(provider, "_create_client", return_value=MagicMock()):
        yield provider


@pytest.fixture
def backup_provider():
    """备份服务商 fixture"""
    provider = BackupAIServiceProvider()
    with patch.object(provider, "_create_client", return_value=MagicMock()):
        yield provider


class TestAIServiceProvider:
    """测试 AIServiceProvider 的 client 重建逻辑"""

    def test_request_client_rebuild_is_idempotent(self, primary_provider):
        """测试 request_client_rebuild() 多次调用只处理一次"""
        primary_provider.request_client_rebuild("test_reason_1")
        primary_provider.request_client_rebuild("test_reason_2")

        assert primary_provider._client_rebuild_pending is True
        assert primary_provider._client_rebuild_reason == "test_reason_1"

    @pytest.mark.asyncio
    async def test_ensure_client_creates_new_client_on_first_call(self, primary_provider):
        """测试首次调用 _ensure_client() 会创建新 client"""
        mock_client = MagicMock()
        with patch.object(primary_provider, "_create_client", return_value=mock_client):
            client = await primary_provider._ensure_client()

        assert client is mock_client
        assert primary_provider.client is mock_client
        assert primary_provider._client_rebuild_pending is False

    @pytest.mark.asyncio
    async def test_ensure_client_reuses_existing_client_when_no_rebuild_pending(
        self, primary_provider
    ):
        """测试没有待重建标记时复用现有 client"""
        mock_client = MagicMock()
        with patch.object(primary_provider, "_create_client", return_value=mock_client):
            client1 = await primary_provider._ensure_client()
            client2 = await primary_provider._ensure_client()

        assert client1 is client2

    @pytest.mark.asyncio
    async def test_ensure_client_rebuilds_when_marked(self, primary_provider):
        """测试有待重建标记时会重建 client"""
        mock_client1 = MagicMock()
        mock_client1.aclose = AsyncMock()

        mock_client2 = MagicMock()

        with patch.object(primary_provider, "_create_client", return_value=mock_client1):
            client1 = await primary_provider._ensure_client()

        # 标记需要重建
        primary_provider.request_client_rebuild("test_rebuild")

        # 再次获取应该是新 client
        with patch.object(primary_provider, "_create_client", return_value=mock_client2):
            client2 = await primary_provider._ensure_client()

        assert client1 is not client2
        assert primary_provider._client_rebuild_pending is False
        mock_client1.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_client_waits_for_in_progress_rebuild_close(self, primary_provider):
        """测试并发调用不会在旧 client 关闭期间绕过待重建状态"""
        old_client = MagicMock()
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def slow_close():
            close_started.set()
            await allow_close.wait()

        old_client.aclose = AsyncMock(side_effect=slow_close)
        new_client = MagicMock()

        primary_provider.client = old_client
        primary_provider._client_created_at = datetime.now()
        primary_provider._client_last_used_at = datetime.now()
        primary_provider.request_client_rebuild("test_rebuild")

        with patch.object(
            primary_provider, "_create_client", return_value=new_client
        ) as create_client:
            task1 = asyncio.create_task(primary_provider._ensure_client())
            await close_started.wait()

            task2 = asyncio.create_task(primary_provider._ensure_client())
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task2), timeout=0.05)
            assert create_client.call_count == 0

            allow_close.set()
            client1, client2 = await asyncio.gather(task1, task2)

        assert client1 is new_client
        assert client2 is new_client
        assert primary_provider.client is new_client
        assert create_client.call_count == 1
        assert primary_provider._client_rebuild_pending is False
        old_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_client_rebuilds_after_idle_timeout(self, primary_provider):
        """测试 client 空闲超时后会重建"""
        old_client = MagicMock()
        old_client.aclose = AsyncMock()
        new_client = MagicMock()

        with patch.object(primary_provider, "_create_client", return_value=old_client):
            await primary_provider._ensure_client()

        primary_provider._client_last_used_at = datetime.now() - timedelta(hours=2)

        with patch.object(primary_provider, "_create_client", return_value=new_client):
            rebuilt = await primary_provider._ensure_client()

        assert rebuilt is new_client
        assert primary_provider.client is new_client
        assert primary_provider._last_client_rebuild_reason == "idle_timeout"
        old_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_client_rebuilds_after_max_lifetime(self, primary_provider):
        """测试 client 超过最大存活时间后会重建"""
        old_client = MagicMock()
        old_client.aclose = AsyncMock()
        new_client = MagicMock()

        with patch.object(primary_provider, "_create_client", return_value=old_client):
            await primary_provider._ensure_client()

        primary_provider._client_created_at = datetime.now() - timedelta(hours=25)
        primary_provider._client_last_used_at = datetime.now() - timedelta(minutes=5)

        with patch.object(primary_provider, "_create_client", return_value=new_client):
            rebuilt = await primary_provider._ensure_client()

        assert rebuilt is new_client
        assert primary_provider.client is new_client
        assert primary_provider._last_client_rebuild_reason == "max_lifetime_exceeded"
        old_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_lifetime_exceeded_wins_over_idle_timeout(self, primary_provider):
        """测试最大存活时间重建优先于空闲超时"""
        old_client = MagicMock()
        old_client.aclose = AsyncMock()

        with patch.object(primary_provider, "_create_client", return_value=old_client):
            await primary_provider._ensure_client()

        primary_provider._client_created_at = datetime.now() - timedelta(hours=25)
        primary_provider._client_last_used_at = datetime.now() - timedelta(hours=2)

        with (
            patch.object(
                primary_provider,
                "request_client_rebuild",
                wraps=primary_provider.request_client_rebuild,
            ) as request_rebuild,
            patch.object(primary_provider, "_create_client", return_value=MagicMock()),
        ):
            await primary_provider._ensure_client()

        request_rebuild.assert_called_once_with("max_lifetime_exceeded")

    @pytest.mark.asyncio
    async def test_auto_rebuild_does_not_override_existing_pending_reason(self, primary_provider):
        """测试自动重建不会覆盖已有待消费重建原因"""
        old_client = MagicMock()
        old_client.aclose = AsyncMock()

        with patch.object(primary_provider, "_create_client", return_value=old_client):
            await primary_provider._ensure_client()

        primary_provider._client_created_at = datetime.now() - timedelta(hours=25)
        primary_provider._client_last_used_at = datetime.now() - timedelta(hours=2)
        primary_provider.request_client_rebuild("provider_failure")

        with (
            patch.object(
                primary_provider,
                "request_client_rebuild",
                wraps=primary_provider.request_client_rebuild,
            ) as request_rebuild,
            patch.object(primary_provider, "_create_client", return_value=MagicMock()),
        ):
            await primary_provider._ensure_client()

        request_rebuild.assert_not_called()
        assert primary_provider._last_client_rebuild_reason == "provider_failure"

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, primary_provider):
        """测试 close() 幂等"""
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()

        with patch.object(primary_provider, "_create_client", return_value=mock_client):
            await primary_provider._ensure_client()

        # 第一次关闭
        await primary_provider.close()
        assert primary_provider.client is None
        assert primary_provider._client_rebuild_pending is False
        assert primary_provider._client_created_at is None
        assert primary_provider._client_last_used_at is None

        # 第二次关闭不应该报错
        await primary_provider.close()
        assert primary_provider.client is None

    @pytest.mark.asyncio
    async def test_close_clears_client_lifecycle_timestamps_but_keeps_rebuild_flags(
        self, primary_provider
    ):
        """测试 close() 会清理生命周期时间戳但保留重建标记"""
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()

        with patch.object(primary_provider, "_create_client", return_value=mock_client):
            await primary_provider._ensure_client()
            primary_provider.request_client_rebuild("test_reason")

        await primary_provider.close()

        assert primary_provider.client is None
        # close() 不再清除重建标记，保留给下次 _ensure_client() 使用
        assert primary_provider._client_rebuild_pending is True
        assert primary_provider._client_rebuild_reason == "test_reason"
        assert primary_provider._client_created_at is None
        assert primary_provider._client_last_used_at is None


class TestHybridAIDetector:
    """测试 HybridAIDetector 的熔断与切换逻辑"""

    @pytest.fixture
    def detector(self):
        """创建 HybridAIDetector 实例"""
        return HybridAIDetector(circuit_breaker_threshold=3, circuit_breaker_cooldown_minutes=5)

    @pytest.mark.asyncio
    async def test_primary_failure_marks_for_rebuild(self, detector):
        """测试 primary 失败后会标记待重建"""
        # Mock primary 失败
        with (
            patch.object(
                detector.primary, "detect", side_effect=AIServiceError("primary", "test error")
            ),
            suppress(RuntimeError),
        ):
            await detector.detect("test text")

        assert detector.primary._client_rebuild_pending is True
        assert detector.primary._client_rebuild_reason == "provider_failure"

    @pytest.mark.asyncio
    async def test_circuit_breaker_triggers_rebuild(self, detector):
        """测试达到熔断阈值时会标记重建"""
        # 连续失败直到触发熔断
        for _ in range(detector.circuit_breaker_threshold):
            with (
                patch.object(
                    detector.primary,
                    "detect",
                    side_effect=AIServiceError("primary", "test error"),
                ),
                suppress(RuntimeError),
            ):
                await detector.detect("test text")

        # 最后一次会触发熔断
        assert detector.primary._client_rebuild_reason == "circuit_breaker_tripped"

    @pytest.mark.asyncio
    async def test_cooldown_end_marks_for_rebuild(self, detector):
        """测试冷却结束后会标记待重建"""
        # 手工设置已过 cooldown 的状态
        stats = detector._stats[detector.primary.name]
        stats.consecutive_failures = detector.circuit_breaker_threshold
        stats.last_failure_time = datetime.now() - timedelta(minutes=10)

        # 调用 _is_circuit_open 应该会重置并标记重建
        detector._is_circuit_open(detector.primary)

        assert stats.consecutive_failures == 0
        assert detector.primary._client_rebuild_pending is True
        assert detector.primary._client_rebuild_reason == "cooldown_ended"

    @pytest.mark.asyncio
    async def test_failover_closes_primary_immediately(self, detector):
        """测试切换到 backup 时会立即关闭 primary client"""
        # 确保 primary 有 client（mock）
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        detector.primary.client = mock_client

        # Mock primary 失败、backup 成功
        with (
            patch.object(
                detector.primary, "detect", side_effect=AIServiceError("primary", "test error")
            ),
            patch.object(
                detector.backup,
                "detect",
                return_value=MagicMock(
                    is_spam=False,
                    confidence=0.1,
                    stage="ai_api",
                    reasons=["测试"],
                    details={},
                    provider="backup",
                    attempt_count=1,
                ),
            ),
        ):
            result = await detector.detect("test text")

        # 验证 primary client 被关闭
        assert detector.primary.client is None
        # 标记应该是 provider_failure（首次失败时标记），而不是 switching_to_backup
        # 因为 switching_to_backup 不会覆盖 provider_failure
        assert detector.primary._client_rebuild_pending is True
        # 实际的 reason 可能是 provider_failure 或 circuit_breaker_tripped，只要标记了就行
        assert detector.primary._client_rebuild_reason in [
            "provider_failure",
            "circuit_breaker_tripped",
        ]
        mock_client.aclose.assert_called_once()

        # 验证返回 backup 的结果
        assert result["details"]["provider"] == "backup"

    @pytest.mark.asyncio
    async def test_failover_does_not_close_backup(self, detector):
        """测试切换时不会关闭 backup client"""
        # 确保 backup 有 client
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        detector.backup.client = mock_client

        # Mock primary 失败、backup 成功
        with (
            patch.object(
                detector.primary, "detect", side_effect=AIServiceError("primary", "test error")
            ),
            patch.object(
                detector.backup,
                "detect",
                return_value=MagicMock(
                    is_spam=False,
                    confidence=0.1,
                    stage="ai_api",
                    reasons=["测试"],
                    details={},
                    provider="backup",
                    attempt_count=1,
                ),
            ),
        ):
            await detector.detect("test text")

        # 验证 backup client 没有被关闭
        assert detector.backup.client is mock_client
        assert detector.backup._client_rebuild_pending is False
        mock_client.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_primary_rebuilds_on_next_use_after_failover(self, detector):
        """测试 failover 后下一次 primary 调用会重建 client"""
        # 创建旧的 primary client（mock）
        old_client = MagicMock()
        old_client.aclose = AsyncMock()
        detector.primary.client = old_client

        # 模拟切换到 backup
        with (
            patch.object(
                detector.primary, "detect", side_effect=AIServiceError("primary", "test error")
            ),
            patch.object(
                detector.backup,
                "detect",
                return_value=MagicMock(
                    is_spam=False,
                    confidence=0.1,
                    stage="ai_api",
                    reasons=["测试"],
                    details={},
                    provider="backup",
                    attempt_count=1,
                ),
            ),
        ):
            await detector.detect("test text")

        # 验证标记已设置
        assert detector.primary._client_rebuild_pending is True

        # 创建新的 client
        new_client = MagicMock()

        # 现在 mock _call_api 让 primary 成功（会触发 _ensure_client 重建）
        async def mock_call_api(_text, _use_context_prompt=False):
            # 这个函数会在 detect() 内部被调用
            return {"is_spam": False, "confidence": 0.1, "reason": "测试"}

        with (
            patch.object(detector.primary, "_call_api", side_effect=mock_call_api),
            patch.object(detector.primary, "_create_client", return_value=new_client),
        ):
            # 先调用 _ensure_client 来消费重建标记
            await detector.primary._ensure_client()

        # 验证重建标记已被清除（因为 _ensure_client() 创建了新 client）
        assert detector.primary._client_rebuild_pending is False

    @pytest.mark.asyncio
    async def test_get_stats_includes_client_lifecycle_fields(self, detector):
        """测试 get_stats() 包含 client 生命周期信息"""
        detector.primary.client = MagicMock()
        detector.primary._client_created_at = datetime(2026, 1, 1, 12, 0, 0)
        detector.primary._client_last_used_at = datetime(2026, 1, 1, 12, 5, 0)
        detector.primary._client_rebuild_count = 2
        detector.primary._last_client_rebuild_at = datetime(2026, 1, 2, 12, 0, 0)
        detector.primary._last_client_rebuild_reason = "idle_timeout"

        stats = detector.get_stats()

        assert "primary" in stats
        assert stats["primary"]["client_initialized"] is True
        assert stats["primary"]["client_created_at"] == "2026-01-01T12:00:00"
        assert stats["primary"]["client_last_used_at"] == "2026-01-01T12:05:00"
        assert stats["primary"]["client_rebuild_count"] == 2
        assert stats["primary"]["last_client_rebuild_at"] == "2026-01-02T12:00:00"
        assert stats["primary"]["last_client_rebuild_reason"] == "idle_timeout"
        assert stats["primary"]["client_age_seconds"] is not None
        assert stats["primary"]["client_idle_seconds"] is not None


class TestBackupProvider:
    """测试 BackupAIServiceProvider 的重建逻辑"""

    @pytest.mark.asyncio
    async def test_backup_failure_also_marks_for_rebuild(self):
        """测试 backup 失败也会标记待重建"""
        detector = HybridAIDetector(circuit_breaker_threshold=3, circuit_breaker_cooldown_minutes=5)

        # Mock primary 失败
        with (
            patch.object(
                detector.primary, "detect", side_effect=AIServiceError("primary", "test error")
            ),
            suppress(RuntimeError),
        ):
            await detector.detect("test text")

        # Mock backup 失败
        with (
            patch.object(
                detector.backup, "detect", side_effect=AIServiceError("backup", "test error")
            ),
            suppress(RuntimeError),
        ):
            await detector.detect("test text")

        # 验证 backup 也被标记重建
        assert detector.backup._client_rebuild_pending is True


class TestVisionHelpers:
    """测试 Vision 基础工具：模型判定、读图、支持性检查"""

    def test_is_vision_model_openai(self):
        assert _is_vision_model("gpt-4o-mini")
        assert _is_vision_model("gpt-4o")
        assert _is_vision_model("GPT-4o-mini")  # 大小写不敏感
        assert _is_vision_model("gpt-4-turbo-2024-04-09")

    def test_is_vision_model_other_providers(self):
        assert _is_vision_model("gemini-1.5-pro")
        assert _is_vision_model("claude-3-5-sonnet-latest")
        assert _is_vision_model("qwen2-vl-72b-instruct")
        assert _is_vision_model("pixtral-12b")

    def test_is_vision_model_non_vision(self):
        assert not _is_vision_model("gpt-3.5-turbo")
        assert not _is_vision_model("deepseek-chat")
        assert not _is_vision_model("")

    def test_is_vision_model_strips_provider_prefix(self):
        """兼容 OpenRouter 等网关的 provider 前缀"""
        # 单级前缀（OpenRouter 标准格式）
        assert _is_vision_model("openai/gpt-4o-mini")
        assert _is_vision_model("anthropic/claude-3-5-sonnet-latest")
        assert _is_vision_model("google/gemini-1.5-pro")
        # 多级前缀
        assert _is_vision_model("openrouter/openai/gpt-4o")
        # 前缀带路径，模型本身不支持
        assert not _is_vision_model("openai/gpt-3.5-turbo")
        assert not _is_vision_model("deepseek/deepseek-chat")
        # 边界：尾部斜杠
        assert not _is_vision_model("openai/")

    def test_read_image_as_base64_png(self, tmp_path):
        img_path = tmp_path / "test.png"
        content = b"\x89PNG\r\n\x1a\nfakecontent"
        img_path.write_bytes(content)

        b64, mime, size = _read_image_as_base64(str(img_path))

        import base64 as b64mod

        assert b64mod.b64decode(b64) == content
        assert mime == "image/png"
        assert size == len(content)

    def test_read_image_as_base64_unknown_suffix_fallback_jpeg(self, tmp_path):
        img_path = tmp_path / "test.dat"
        img_path.write_bytes(b"anything")
        _, mime, _ = _read_image_as_base64(str(img_path))
        assert mime == "image/jpeg"

    def test_read_image_as_base64_missing_file(self, tmp_path):
        import pytest

        with pytest.raises(FileNotFoundError):
            _read_image_as_base64(str(tmp_path / "nope.jpg"))


class TestAIServiceProviderVision:
    """测试 AIServiceProvider 的 Vision 能力判定"""

    def test_supports_vision_gpt4o(self, monkeypatch):
        cfg = AIServiceConfig(
            enabled=True, api_key="k", api_base="https://x.test/v1", model="gpt-4o-mini"
        )
        p = PrimaryAIServiceProvider.__new__(PrimaryAIServiceProvider)
        # 复用父类初始化但绕过 settings 依赖
        from src.ml.ai_detector import AIServiceProvider

        AIServiceProvider.__init__(p, "primary", cfg)
        assert p.supports_vision is True

    def test_supports_vision_text_only_model(self):
        cfg = AIServiceConfig(
            enabled=True, api_key="k", api_base="https://x.test/v1", model="deepseek-chat"
        )
        p = PrimaryAIServiceProvider.__new__(PrimaryAIServiceProvider)
        from src.ml.ai_detector import AIServiceProvider

        AIServiceProvider.__init__(p, "primary", cfg)
        assert p.supports_vision is False

    def test_process_vision_result_parses_extracted_text(self):
        cfg = AIServiceConfig(
            enabled=True,
            api_key="k",
            api_base="https://x.test/v1",
            model="gpt-4o-mini",
            threshold=0.8,
        )
        from src.ml.ai_detector import AIServiceProvider

        p = PrimaryAIServiceProvider.__new__(PrimaryAIServiceProvider)
        AIServiceProvider.__init__(p, "primary", cfg)

        raw = {
            "is_spam": True,
            "confidence": 0.95,
            "reason": "赌博广告",
            "extracted_text": "稳赚不赔 加微信 abc123",
        }
        detection = p._process_vision_result(raw)
        assert detection.is_spam is True
        assert detection.confidence == 0.95
        assert detection.stage == "ai_vision"
        assert detection.details["extracted_text"] == "稳赚不赔 加微信 abc123"
        assert detection.reasons == ["赌博广告"]

    def test_process_vision_result_below_threshold(self):
        cfg = AIServiceConfig(
            enabled=True,
            api_key="k",
            api_base="https://x.test/v1",
            model="gpt-4o-mini",
            threshold=0.8,
        )
        from src.ml.ai_detector import AIServiceProvider

        p = PrimaryAIServiceProvider.__new__(PrimaryAIServiceProvider)
        AIServiceProvider.__init__(p, "primary", cfg)

        raw = {"is_spam": True, "confidence": 0.5, "reason": "轻微可疑"}
        detection = p._process_vision_result(raw)
        # 置信度低于阈值 → is_spam 最终为 False
        assert detection.is_spam is False
        assert detection.confidence == 0.5
        assert detection.details["extracted_text"] == ""
