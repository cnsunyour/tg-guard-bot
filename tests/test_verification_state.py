"""验证状态层测试（verify_choice_answer/verify_answer 三态 + claim_success 集成）。

verify_* 内部 MGET(main, deadline) 快照，答案正确时调 claim_success 原子消费状态；
claim 失败（timeout 已接管 / session 切换）返回 expired，调用方静默退出。
"""

from unittest.mock import AsyncMock

import pytest

from src.core.redis import RedisKeys
from src.services import verification
from src.services.verification import VerificationService

pytestmark = pytest.mark.unit

DEADLINE = "session-a:1120000"


@pytest.mark.parametrize(
    ("stored", "expected_type", "answer", "expected"),
    [
        ("qa:2", "qa", "2", "correct"),
        ("qa:2", "qa", "1", "wrong"),
        ("honeypot:8", "honeypot", "trap", "wrong"),
        ("honeypot:8", "honeypot", "8", "correct"),
        ("math:5", "math", "5", "correct"),
        # 跨类型重放（旧 slider 按钮点当前 qa 挑战，stored 答案碰巧相同）→ expired
        ("qa:2", "slider", "2", "expired"),
        (None, "qa", "2", "expired"),  # 无 pending
        ("invalid", "qa", "2", "expired"),  # 无冒号
        ("qa:", "qa", "2", "expired"),  # 空 answer
        (":2", "qa", "2", "expired"),  # 空 type
        ("qa:2:extra", "qa", "2", "expired"),  # answer 含冒号
    ],
)
async def test_verify_choice_answer(
    mocker, stored: str | None, expected_type: str, answer: str, expected: str
) -> None:
    redis = AsyncMock()
    redis.mget.return_value = [stored, DEADLINE]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    success_claim = mocker.patch.object(
        verification,
        "claim_success",
        new=AsyncMock(return_value=True),
    )

    result = await VerificationService.verify_choice_answer(-100, 42, expected_type, answer)

    assert result == expected
    redis.mget.assert_awaited_once_with(
        RedisKeys.verification(-100, 42),
        RedisKeys.verification_deadline(-100, 42),
    )
    if expected == "correct":
        success_claim.assert_awaited_once_with(-100, 42, stored, DEADLINE)
    else:
        success_claim.assert_not_awaited()


async def test_verify_choice_answer_returns_expired_when_success_claim_loses(mocker) -> None:
    """答案正确但 timeout 已先 claim 时返回 expired（不恢复权限也不处罚）。"""
    redis = AsyncMock()
    redis.mget.return_value = ["qa:2", DEADLINE]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    success_claim = mocker.patch.object(
        verification,
        "claim_success",
        new=AsyncMock(return_value=False),
    )

    result = await VerificationService.verify_choice_answer(-100, 42, "qa", "2")

    assert result == "expired"
    success_claim.assert_awaited_once_with(-100, 42, "qa:2", DEADLINE)


@pytest.mark.parametrize(
    ("stored", "answer", "claim_granted", "expected", "should_claim"),
    [
        ("captcha:ABCD", "abcd", True, "correct", True),  # 大小写不敏感 + claim 成功
        ("captcha:ABCD", "ABCD", False, "expired", True),  # claim 失败（timeout 接管）
        ("captcha:ABCD", "wrong", True, "wrong", False),  # 答案错
        (None, "ABCD", True, "expired", False),  # 无 pending
        ("qa:ABCD", "ABCD", True, "expired", False),  # 非 captcha 类型
        ("invalid", "ABCD", True, "expired", False),  # 无冒号
        ("captcha:", "", True, "expired", False),  # 空答案
        ("captcha:AB:CD", "AB:CD", True, "expired", False),  # 答案含冒号
    ],
)
async def test_verify_answer_three_state(
    mocker,
    stored: str | None,
    answer: str,
    claim_granted: bool,
    expected: str,
    should_claim: bool,
) -> None:
    redis = AsyncMock()
    redis.mget.return_value = [stored, DEADLINE]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    success_claim = mocker.patch.object(
        verification,
        "claim_success",
        new=AsyncMock(return_value=claim_granted),
    )

    result = await VerificationService.verify_answer(-100, 42, answer)

    assert result == expected
    redis.mget.assert_awaited_once_with(
        RedisKeys.verification(-100, 42),
        RedisKeys.verification_deadline(-100, 42),
    )
    if should_claim:
        success_claim.assert_awaited_once_with(-100, 42, stored, DEADLINE)
    else:
        success_claim.assert_not_awaited()
