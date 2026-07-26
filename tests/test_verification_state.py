"""验证状态层测试（verify_choice_answer 类型校验，防跨类型旧消息重放）。"""

from unittest.mock import AsyncMock

import pytest

from src.core.redis import RedisKeys
from src.services.verification import VerificationService

pytestmark = pytest.mark.unit


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
    redis.get.return_value = stored
    mocker.patch("src.services.verification.get_redis", return_value=redis)

    result = await VerificationService.verify_choice_answer(-100, 42, expected_type, answer)

    assert result == expected
    redis.get.assert_awaited_once_with(RedisKeys.verification(-100, 42))
