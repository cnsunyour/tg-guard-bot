"""spam_detector retrain_model / check_and_auto_train code 化测试(3c11)。

验证 RetrainResult 4 code 分支 + success 计算属性 +
check_and_auto_train None/Result 语义 + _notify_admins 逐人 locale 渲染。
"""

from unittest.mock import AsyncMock, MagicMock, patch

from src.services.spam_detector import RetrainCode, RetrainResult, SpamDetector


def _make_detector() -> SpamDetector:
    """构造 SpamDetector(依赖 mock,不实际加载模型)。"""
    with (
        patch("src.services.spam_detector.get_rule_engine"),
        patch("src.services.spam_detector.get_classifier"),
        patch("src.services.spam_detector.get_embedder"),
        patch("src.services.spam_detector.get_ai_detector"),
    ):
        return SpamDetector()


# ===== RetrainResult 计算属性 =====
def test_retrain_result_success_only_when_success_code() -> None:
    """success 仅在 code=success 时为 True。"""
    assert RetrainResult(code=RetrainCode.success, params={}).success is True
    for code in (RetrainCode.insufficient_samples, RetrainCode.save_failed, RetrainCode.failed):
        assert RetrainResult(code=code, params={}).success is False


def test_retrain_code_value_matches_string() -> None:
    """StrEnum value 是稳定字符串(catalog key 依赖)。"""
    assert RetrainCode.success.value == "success"
    assert RetrainCode.insufficient_samples.value == "insufficient_samples"
    assert RetrainCode.save_failed.value == "save_failed"
    assert RetrainCode.failed.value == "failed"


# ===== retrain_model 4 code 分支 =====
async def test_retrain_insufficient_samples(mocker) -> None:
    """样本 < 10 → insufficient_samples,{current}/{min_required}。"""
    detector = _make_detector()
    mocker.patch(
        "src.services.spam_detector.SpamRepository.get_training_data",
        new=AsyncMock(return_value=(["t"] * 3, [1] * 3)),
    )

    result = await detector.retrain_model()

    assert result.code is RetrainCode.insufficient_samples
    assert result.success is False
    assert result.params["current"] == 3
    assert result.params["min_required"] == 10


async def test_retrain_save_failed(mocker) -> None:
    """训练成功但 save_model 返回 False → save_failed。"""
    detector = _make_detector()
    detector.classifier.train = MagicMock(
        return_value=(0.95, {"total_samples": 100, "spam_samples": 30, "normal_samples": 70})
    )
    detector.classifier.save_model = MagicMock(return_value=False)
    mocker.patch(
        "src.services.spam_detector.SpamRepository.get_training_data",
        new=AsyncMock(return_value=(["t"] * 10, [1] * 10)),
    )

    result = await detector.retrain_model()

    assert result.code is RetrainCode.save_failed
    assert result.success is False
    assert result.params == {}


async def test_retrain_success(mocker) -> None:
    """合法 → success,{accuracy}/{total}/{spam}/{normal}。"""
    detector = _make_detector()
    detector.classifier.train = MagicMock(
        return_value=(0.95, {"total_samples": 100, "spam_samples": 30, "normal_samples": 70})
    )
    detector.classifier.save_model = MagicMock(return_value=True)
    mocker.patch(
        "src.services.spam_detector.SpamRepository.get_training_data",
        new=AsyncMock(return_value=(["t"] * 10, [1] * 10)),
    )
    # retrain_model 内局部 import,mock 源模块
    mocker.patch("src.repositories.spam_repo.update_last_train_count")
    mock_redis = MagicMock()
    mock_redis.set = AsyncMock()
    mocker.patch("src.core.redis.get_redis", return_value=mock_redis)

    result = await detector.retrain_model()

    assert result.code is RetrainCode.success
    assert result.success is True
    assert result.params["accuracy"] == 0.95
    assert result.params["total_samples"] == 100
    assert result.params["spam_samples"] == 30
    assert result.params["normal_samples"] == 70


async def test_retrain_failed_on_exception(mocker) -> None:
    """get_training_data 抛异常 → failed,{error}。"""
    detector = _make_detector()
    mocker.patch(
        "src.services.spam_detector.SpamRepository.get_training_data",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    )

    result = await detector.retrain_model()

    assert result.code is RetrainCode.failed
    assert result.success is False
    assert "db down" in str(result.params["error"])


# ===== check_and_auto_train None/Result 语义 =====
async def test_auto_train_cooldown_returns_none(mocker) -> None:
    """冷却中 → None。"""
    detector = _make_detector()
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value="1000.0")  # 最近训练过
    mock_redis.set = AsyncMock()
    mocker.patch("src.core.redis.get_redis", return_value=mock_redis)
    mocker.patch.object(detector, "retrain_model", new=AsyncMock())

    result = await detector.check_and_auto_train()

    assert result is None
    detector.retrain_model.assert_not_awaited()


async def test_auto_train_below_threshold_returns_none(mocker) -> None:
    """未达阈值 → None。"""
    detector = _make_detector()
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)  # 无冷却
    mock_redis.set = AsyncMock()
    mocker.patch("src.core.redis.get_redis", return_value=mock_redis)
    mocker.patch(
        "src.services.spam_detector.SpamRepository.count_samples", new=AsyncMock(return_value=5)
    )
    mocker.patch("src.repositories.spam_repo.get_last_train_count", return_value=0)

    result = await detector.check_and_auto_train(threshold=100)

    assert result is None


async def test_auto_train_triggers_returns_result(mocker) -> None:
    """达阈值 → 调 retrain_model 返回其 RetrainResult。"""
    detector = _make_detector()
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()
    mocker.patch("src.core.redis.get_redis", return_value=mock_redis)
    mocker.patch(
        "src.services.spam_detector.SpamRepository.count_samples", new=AsyncMock(return_value=50)
    )
    mocker.patch("src.repositories.spam_repo.get_last_train_count", return_value=0)
    expected = RetrainResult(code=RetrainCode.success, params={"accuracy": 0.9})
    mocker.patch.object(detector, "retrain_model", new=AsyncMock(return_value=expected))

    result = await detector.check_and_auto_train(threshold=10)

    assert result is expected
    # success 时更新训练时间
    mock_redis.set.assert_awaited()


async def test_auto_train_check_exception_returns_none(mocker) -> None:
    """check 过程异常 → None(不传播,仅日志)。"""
    detector = _make_detector()
    mocker.patch("src.core.redis.get_redis", side_effect=RuntimeError("redis down"))

    result = await detector.check_and_auto_train()

    assert result is None


# ===== _notify_admins 逐人 locale =====
async def test_notify_admins_per_admin_locale(mocker) -> None:
    """每个管理员用各自 for_user locale 渲染;session 始终关闭。"""
    detector = _make_detector()
    result = RetrainResult(
        code=RetrainCode.success,
        params={"accuracy": 0.95, "total_samples": 100, "spam_samples": 30, "normal_samples": 70},
    )
    localizer = MagicMock()
    localizer.t = MagicMock(return_value="notification")
    resolver = MagicMock()
    # 两管理员不同 locale
    resolver.for_user = AsyncMock(side_effect=["zh-Hans", "en"])
    translator = MagicMock()
    translator.for_locale = MagicMock(return_value=localizer)
    mocker.patch("src.core.i18n.get_resolver", return_value=resolver)
    mocker.patch("src.core.i18n.get_translator", return_value=translator)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()
    mocker.patch("aiogram.Bot", return_value=mock_bot)

    await detector._notify_admins_training_complete([1, 2], result)

    # 两管理员各自 for_user
    assert resolver.for_user.await_count == 2
    # 两通知发送
    assert mock_bot.send_message.await_count == 2
    # session 始终关闭
    mock_bot.session.close.assert_awaited()
    # notification key + accuracy_percent 格式化
    t_call = localizer.t.call_args
    assert t_call.args == ("admin.antispam.auto_train.notification.success.message",)
    assert t_call.kwargs["accuracy_percent"] == "95.00%"


async def test_notify_admins_single_failure_does_not_block_others(mocker) -> None:
    """单管理员发送失败不阻断其他管理员 + session 关闭。"""
    detector = _make_detector()
    result = RetrainResult(
        code=RetrainCode.success,
        params={"accuracy": 0.9, "total_samples": 10, "spam_samples": 3, "normal_samples": 7},
    )
    localizer = MagicMock()
    localizer.t = MagicMock(return_value="notification")
    resolver = MagicMock()
    resolver.for_user = AsyncMock(return_value="zh-Hans")
    translator = MagicMock()
    translator.for_locale = MagicMock(return_value=localizer)
    mocker.patch("src.core.i18n.get_resolver", return_value=resolver)
    mocker.patch("src.core.i18n.get_translator", return_value=translator)

    mock_bot = MagicMock()
    # 第一个管理员发送抛异常,第二个成功
    mock_bot.send_message = AsyncMock(side_effect=[RuntimeError("net"), None])
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()
    mocker.patch("aiogram.Bot", return_value=mock_bot)

    await detector._notify_admins_training_complete([1, 2], result)

    # 两管理员都尝试
    assert mock_bot.send_message.await_count == 2
    # session 关闭
    mock_bot.session.close.assert_awaited()
