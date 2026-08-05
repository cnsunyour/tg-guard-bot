"""验证状态层测试（verify_choice_answer/verify_answer 状态 + verdict claim 集成）。

verify_* 内部 MGET(main, deadline) 快照，答案正确/错误分别调 claim_success/claim_failure
原子消费状态；claim 成功返回对应 status + flow，失败（timeout 已接管 / session 切换）返回 expired。
"""

from unittest.mock import AsyncMock

import pytest

from src.core.redis import RedisKeys
from src.services import verification
from src.services.verification import ChoiceAnswerResult, VerificationService, VerifyResult
from src.services.verification_recovery import VerificationFlow

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
    mocker, stored: str | None, expected_type: str, answer: str, expected: ChoiceAnswerResult
) -> None:
    redis = AsyncMock()
    redis.mget.return_value = [stored, DEADLINE]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    success_claim = mocker.patch.object(
        verification,
        "claim_success",
        new=AsyncMock(return_value="join_request"),
    )
    failure_claim = mocker.patch.object(
        verification,
        "claim_failure",
        new=AsyncMock(return_value="join_request"),
    )

    result = await VerificationService.verify_choice_answer(-100, 42, expected_type, answer)

    assert result == VerifyResult(
        status=expected,
        flow="join_request" if expected in ("correct", "wrong") else None,
    )
    redis.mget.assert_awaited_once_with(
        RedisKeys.verification(-100, 42),
        RedisKeys.verification_deadline(-100, 42),
    )
    if expected == "correct":
        success_claim.assert_awaited_once_with(-100, 42, stored, DEADLINE)
        failure_claim.assert_not_awaited()
    elif expected == "wrong":
        failure_claim.assert_awaited_once_with(-100, 42, stored, DEADLINE)
        success_claim.assert_not_awaited()
    else:
        success_claim.assert_not_awaited()
        failure_claim.assert_not_awaited()


async def test_verify_choice_answer_returns_expired_when_success_claim_loses(mocker) -> None:
    """答案正确但 timeout 已先 claim 时返回 expired（不恢复权限也不处罚）。"""
    redis = AsyncMock()
    redis.mget.return_value = ["qa:2", DEADLINE]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    success_claim = mocker.patch.object(
        verification,
        "claim_success",
        new=AsyncMock(return_value=None),
    )

    result = await VerificationService.verify_choice_answer(-100, 42, "qa", "2")

    assert result == VerifyResult(status="expired")
    success_claim.assert_awaited_once_with(-100, 42, "qa:2", DEADLINE)


async def test_verify_choice_answer_returns_expired_when_failure_claim_loses(mocker) -> None:
    """答案错误但 timeout 已先 claim 时返回 expired，不再执行失败处罚。"""
    redis = AsyncMock()
    redis.mget.return_value = ["qa:2", DEADLINE]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    failure_claim = mocker.patch.object(
        verification,
        "claim_failure",
        new=AsyncMock(return_value=None),
    )

    result = await VerificationService.verify_choice_answer(-100, 42, "qa", "1")

    assert result == VerifyResult(status="expired")
    failure_claim.assert_awaited_once_with(-100, 42, "qa:2", DEADLINE)


@pytest.mark.parametrize(
    ("stored", "answer", "claim_flow", "expected", "claim_kind"),
    [
        ("captcha:ABCD", "abcd", "join", "correct", "success"),  # 大小写不敏感 + claim 成功
        ("captcha:ABCD", "ABCD", None, "expired", "success"),  # claim 失败（timeout 接管）
        ("captcha:ABCD", "wrong", "join_request", "wrong", "failure"),  # 答案错 → failure claim
        ("captcha:ABCD", "wrong", None, "expired", "failure"),  # failure claim 失败
        (None, "ABCD", "join", "expired", None),  # 无 pending
        ("qa:ABCD", "ABCD", "join", "expired", None),  # 非 captcha 类型
        ("invalid", "ABCD", "join", "expired", None),  # 无冒号
        ("captcha:", "", "join", "expired", None),  # 空答案
        ("captcha:AB:CD", "AB:CD", "join", "expired", None),  # 答案含冒号
    ],
)
async def test_verify_answer_three_state(
    mocker,
    stored: str | None,
    answer: str,
    claim_flow: VerificationFlow | None,
    expected: ChoiceAnswerResult,
    claim_kind: str | None,
) -> None:
    redis = AsyncMock()
    redis.mget.return_value = [stored, DEADLINE]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    success_claim = mocker.patch.object(
        verification,
        "claim_success",
        new=AsyncMock(return_value=claim_flow),
    )
    failure_claim = mocker.patch.object(
        verification,
        "claim_failure",
        new=AsyncMock(return_value=claim_flow),
    )

    result = await VerificationService.verify_answer(
        -100, 42, answer, expected_deadline_value=DEADLINE
    )

    assert result == VerifyResult(
        status=expected,
        flow=claim_flow if expected in ("correct", "wrong") else None,
    )
    redis.mget.assert_awaited_once_with(
        RedisKeys.verification(-100, 42),
        RedisKeys.verification_deadline(-100, 42),
    )
    if claim_kind == "success":
        success_claim.assert_awaited_once_with(-100, 42, stored, DEADLINE)
        failure_claim.assert_not_awaited()
    elif claim_kind == "failure":
        failure_claim.assert_awaited_once_with(-100, 42, stored, DEADLINE)
        success_claim.assert_not_awaited()
    else:
        success_claim.assert_not_awaited()
        failure_claim.assert_not_awaited()


async def test_verify_answer_rejects_session_switch_before_comparing_answer(mocker) -> None:
    """waiting 捕获旧 deadline 后切到新 session：不得用新 main 返回 wrong/correct。"""
    redis = AsyncMock()
    redis.mget.return_value = ["captcha:NEWCODE", "session-b:1240000"]
    mocker.patch.object(verification, "get_redis", return_value=redis)
    success_claim = mocker.patch.object(
        verification,
        "claim_success",
        new=AsyncMock(return_value="join"),
    )
    failure_claim = mocker.patch.object(
        verification,
        "claim_failure",
        new=AsyncMock(return_value="join"),
    )

    result = await VerificationService.verify_answer(
        -100, 42, "OLDCODE", expected_deadline_value=DEADLINE
    )

    assert result == VerifyResult(status="expired")
    success_claim.assert_not_awaited()
    failure_claim.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "flow"),
    [
        ("correct", None),  # correct 必须携带 flow
        ("wrong", None),  # wrong 必须携带 flow
        ("expired", "join_request"),  # expired 不得携带 flow
    ],
)
def test_verify_result_rejects_inconsistent_flow(
    status: ChoiceAnswerResult, flow: VerificationFlow | None
) -> None:
    """VerifyResult 强制 correct/wrong 携带 flow、expired 不得携带 flow。"""
    with pytest.raises(ValueError, match="correct/wrong 必须携带 flow"):
        VerifyResult(status=status, flow=flow)
