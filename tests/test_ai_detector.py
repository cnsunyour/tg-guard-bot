"""AI 检测器单元测试

测试主备引擎的 client 重建、熔断、切换逻辑
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.ml.ai_detector import (
    AIServiceConfig,
    AIServiceError,
    AIServiceProvider,
    BackupAIServiceProvider,
    HybridAIDetector,
    PrimaryAIServiceProvider,
    VisionServiceProvider,
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
    """测试 Vision 图片编码工具"""

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


class TestVisionServiceProviderAvailability:
    """Vision provider 可用性取决于开关、API key 与非空模型名（不按模型名预校验能力）"""

    @pytest.mark.parametrize(
        "model",
        [
            "deepseek-chat",  # 原白名单外的文本模型名
            "some-future-vision-model",  # 任意未知名的新模型
            "openai/gpt-4o-mini",  # 带 provider 前缀
        ],
    )
    def test_is_available_does_not_filter_by_model_name(self, model):
        """移除白名单后：任意模型名只要启用且配置了 key 即视为可用"""
        cfg = AIServiceConfig(
            enabled=True,
            api_key="k",
            api_base="https://x.test/v1",
            model=model,
        )
        provider = VisionServiceProvider("vision", cfg)
        assert provider.is_available is True

    @pytest.mark.parametrize(
        ("enabled", "api_key", "expected"),
        [
            (False, "k", False),
            (True, "", False),
            (True, "k", True),
        ],
    )
    def test_is_available_requires_enabled_and_api_key(self, enabled, api_key, expected):
        cfg = AIServiceConfig(
            enabled=enabled,
            api_key=api_key,
            api_base="https://x.test/v1",
            model="arbitrary-model",
        )
        provider = VisionServiceProvider("vision", cfg)
        assert provider.is_available is expected

    @pytest.mark.parametrize("model", ["", "   "])
    def test_is_available_rejects_empty_model_name(self, model):
        """空模型名/纯空白视为不可用（避免带空 model 反复调用无效 API）"""
        cfg = AIServiceConfig(
            enabled=True,
            api_key="k",
            api_base="https://x.test/v1",
            model=model,
        )
        provider = VisionServiceProvider("vision", cfg)
        assert provider.is_available is False


class TestAIServiceProviderVisionProcessing:
    """测试 AIServiceProvider 的 Vision 响应处理"""

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


# ============================================================================
# _format_error 输出回归保护
# 防止公共格式化器（src.core.http_errors）日后改动悄然改变 AI 日志格式
# ============================================================================


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout(""), "ReadTimeout [phase=read]"),
        (httpx.ConnectTimeout(""), "ConnectTimeout [phase=connect]"),
        (httpx.WriteTimeout(""), "WriteTimeout [phase=write]"),
        (httpx.PoolTimeout(""), "PoolTimeout [phase=pool]"),
        (httpx.ReadTimeout("timed out"), "ReadTimeout [phase=read] [message=timed out]"),
        (httpx.ConnectError(""), "ConnectError"),
        (httpx.ConnectError("connection refused"), "ConnectError [message=connection refused]"),
        (ValueError("无法解析"), "ValueError [message=无法解析]"),
    ],
)
def test_format_error_output_stable(error, expected):
    """_format_error 对各类异常输出稳定，锁定改造前的历史格式"""
    assert AIServiceProvider._format_error(error) == expected


def test_format_error_aiservice_error_keeps_provider():
    """AIServiceError 保留 provider 与 message（领域异常不走公共格式化器）"""
    error = AIServiceError("primary", "所有重试失败")
    assert (
        AIServiceProvider._format_error(error)
        == "AIServiceError [provider=primary] [message=所有重试失败]"
    )


def test_format_error_http_status_error_raw_mode():
    """HTTPStatusError 提取状态码、响应原文与 message（raw 模式保持历史格式）"""
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(429, text='{"error":{"message":"rate limited"}}', request=request)
    error = httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)
    assert (
        AIServiceProvider._format_error(error)
        == 'HTTPStatusError [status_code=429] [response={"error":{"message":"rate limited"}}] '
        "[message=429 Too Many Requests]"
    )
