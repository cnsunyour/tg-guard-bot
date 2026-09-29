"""自定义脚本验证的服务层状态机测试（mock Redis/沙盒，无真实网络）。"""

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from src.core.redis import RedisKeys
from src.services import verification as service
from src.services.sandbox_client import SandboxProtocolError, SandboxUnavailableError
from src.services.verification import (
    ScriptChallenge,
    ScriptPrepareFallback,
    ScriptVerifyUnavailable,
    VerificationService,
)

if TYPE_CHECKING:
    from src.services.verification import PreparedChallenge
    from src.services.verification_recovery import RecoveryReservation

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CHAT = -100123
USER = 42
SESSION = "session-A"
DEADLINE = f"{SESSION}:2000"


def _script_payload() -> dict:
    return {
        "revision_id": 7,
        "source_sha256": "a" * 64,
        "language": "python",
        "state": {"answer": "B"},
        "option_values": ["v1", "v2"],
        "locale": "zh-Hans",
        "issued_at_ms": 1000,
        "expires_at_ms": 2000,
        "username": " tester ",
    }


def _token(payload: dict) -> str:
    # SESSION 与 DEADLINE 常量的 session 段一致（snapshot 重算取 deadline 快照的 session）
    return VerificationService._script_state_token(
        SESSION, payload["revision_id"], payload["state"], payload["option_values"]
    )


class Env:
    """verify_script_answer 的公共 mock 环境。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.payload = _script_payload()
        self.main = _token(self.payload)
        self.redis = SimpleNamespace(
            mget=AsyncMock(return_value=[self.main, DEADLINE]),
            get=AsyncMock(return_value=json.dumps(self.payload)),
        )
        self.revision = SimpleNamespace(
            id=7, language="python", source="SRC", source_sha256="a" * 64
        )
        self.client = SimpleNamespace(
            execute_verify=AsyncMock(return_value=SimpleNamespace(decision="pass")),
            execute_ask=AsyncMock(),
        )
        self.get_group_revision = AsyncMock(return_value=self.revision)
        self.claim_success = AsyncMock(return_value="join")
        self.claim_failure = AsyncMock(return_value="join")
        monkeypatch.setattr(service, "get_redis", lambda: self.redis)
        monkeypatch.setattr(service, "claim_success", self.claim_success)
        monkeypatch.setattr(service, "claim_failure", self.claim_failure)
        monkeypatch.setattr(service, "RedisKeys", service.RedisKeys)  # 真实键生成（无需 mock）
        monkeypatch.setattr(
            "src.repositories.custom_verification_repo.CustomVerificationRepository.get_group_revision",
            self.get_group_revision,
        )
        monkeypatch.setattr("src.services.sandbox_client.get_sandbox_client", lambda: self.client)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(monkeypatch)


async def _verify(env: Env, answer: str = "v1", token: str | None = None):
    return await VerificationService.verify_script_answer(
        CHAT,
        USER,
        answer,
        expected_deadline_value=DEADLINE,
        expected_state_token=token if token is not None else env.main.partition(":")[2],
    )


class TestVerifyScriptAnswer:
    async def test_pass_claims_success_with_snapshot(self, env: Env):
        result = await _verify(env)
        assert result.status == "correct"
        assert result.flow == "join"
        # 群绑定读取：revision 查询必须带当前群（拒绝跨群注入）
        env.get_group_revision.assert_awaited_once_with(CHAT, 7)
        env.claim_success.assert_awaited_once_with(CHAT, USER, env.main, DEADLINE)
        env.claim_failure.assert_not_awaited()
        # ctx 组装：state 回传 + input 注入
        ctx = env.client.execute_verify.await_args.args[2]
        assert ctx["state"] == {"answer": "B"}
        assert ctx["input"] == "v1"
        assert ctx["challenge_id"] == f"{CHAT}:{USER}:7"

    async def test_retry_claims_failure(self, env: Env):
        env.client.execute_verify.return_value = SimpleNamespace(decision="retry")
        result = await _verify(env)
        assert result.status == "wrong"
        env.claim_failure.assert_awaited_once_with(CHAT, USER, env.main, DEADLINE)
        env.claim_success.assert_not_awaited()

    @pytest.mark.parametrize(
        "error", [SandboxUnavailableError("down"), SandboxProtocolError("bad")]
    )
    async def test_sandbox_failure_raises_and_never_claims(self, env: Env, error):
        env.client.execute_verify.side_effect = error
        with pytest.raises(ScriptVerifyUnavailable):
            await _verify(env)
        env.claim_success.assert_not_awaited()
        env.claim_failure.assert_not_awaited()

    async def test_deadline_binding_rejects_before_any_io(self, env: Env):
        env.redis.mget.return_value = [env.main, "session-B:2000"]
        result = await _verify(env)
        assert result.status == "expired"
        env.redis.get.assert_not_awaited()
        env.client.execute_verify.assert_not_awaited()

    async def test_missing_script_state_is_expired(self, env: Env):
        env.redis.get.return_value = None
        result = await _verify(env)
        assert result.status == "expired"
        env.client.execute_verify.assert_not_awaited()

    async def test_corrupt_script_state_is_expired(self, env: Env):
        env.redis.get.return_value = "{not json"
        assert (await _verify(env)).status == "expired"

    async def test_token_mismatch_is_expired(self, env: Env):
        # 脚本状态键与主键不同源（迟到写入/跨会话错配）：token 重算不匹配
        env.payload["state"] = {"answer": "C"}
        env.redis.get.return_value = json.dumps(env.payload)
        assert (await _verify(env)).status == "expired"
        env.client.execute_verify.assert_not_awaited()

    async def test_wrong_type_prefix_is_expired(self, env: Env):
        env.redis.mget.return_value = ["qa:2", DEADLINE]
        assert (await _verify(env)).status == "expired"

    async def test_stale_callback_token_is_expired(self, env: Env):
        # 旧验证消息按钮的 token 与当前主键不匹配：拦截为 expired，不读状态不执行沙盒
        result = await _verify(env, token="0" * 16)
        assert result.status == "expired"
        env.redis.get.assert_not_awaited()
        env.client.execute_verify.assert_not_awaited()

    async def test_nan_in_state_is_expired(self, env: Env):
        env.redis.get.return_value = '{"revision_id": 7, "state": {"x": NaN}}'
        assert (await _verify(env)).status == "expired"
        env.client.execute_verify.assert_not_awaited()

    async def test_missing_payload_fields_are_expired(self, env: Env):
        env.redis.get.return_value = json.dumps({"revision_id": 7, "state": None})
        assert (await _verify(env)).status == "expired"
        env.client.execute_verify.assert_not_awaited()

    async def test_claim_race_lost_returns_expired(self, env: Env):
        env.claim_success.return_value = None
        assert (await _verify(env)).status == "expired"

    async def test_missing_revision_raises_unavailable(self, env: Env):
        env.get_group_revision.return_value = None
        with pytest.raises(ScriptVerifyUnavailable):
            await _verify(env)
        env.client.execute_verify.assert_not_awaited()

    async def test_revision_content_drift_raises_unavailable(self, env: Env):
        env.revision.source_sha256 = "b" * 64
        with pytest.raises(ScriptVerifyUnavailable):
            await _verify(env)


class TestResolveScriptOption:
    async def test_index_maps_to_value(self, env: Env):
        value = await VerificationService.resolve_script_option(
            CHAT,
            USER,
            "1",
            expected_deadline_value=DEADLINE,
            expected_state_token=env.main.partition(":")[2],
        )
        assert value == "v2"

    @pytest.mark.parametrize("bad_index", ["-1", "99", "abc", "", "1:2", "9" * 5])
    async def test_invalid_index_returns_none(self, env: Env, bad_index: str):
        assert (
            await VerificationService.resolve_script_option(
                CHAT,
                USER,
                bad_index,
                expected_deadline_value=DEADLINE,
                expected_state_token=env.main.partition(":")[2],
            )
            is None
        )


class TestPrepareScriptChallenge:
    async def test_ask_failure_raises_fallback(self, monkeypatch: pytest.MonkeyPatch):
        client = SimpleNamespace(execute_ask=AsyncMock(side_effect=SandboxUnavailableError("down")))
        monkeypatch.setattr("src.services.sandbox_client.get_sandbox_client", lambda: client)
        monkeypatch.setattr(
            "src.repositories.custom_verification_repo.CustomVerificationRepository.get_active_revision",
            AsyncMock(
                return_value=SimpleNamespace(id=7, language="python", source="S", source_sha256="a")
            ),
        )
        group = SimpleNamespace(verification_timeout=120)
        with pytest.raises(ScriptPrepareFallback):
            await VerificationService.prepare_script_challenge(
                group, CHAT, USER, session_id=SESSION, locale="zh-Hans"
            )

    async def test_no_active_revision_raises_fallback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "src.repositories.custom_verification_repo.CustomVerificationRepository.get_active_revision",
            AsyncMock(return_value=None),
        )
        group = SimpleNamespace(verification_timeout=120)
        with pytest.raises(ScriptPrepareFallback):
            await VerificationService.prepare_script_challenge(
                group, CHAT, USER, session_id=SESSION, locale="zh-Hans"
            )

    async def test_normal_ask_builds_challenge_and_state(self, monkeypatch: pytest.MonkeyPatch):
        ask_result = SimpleNamespace(
            text="1+1=?",
            options=[SimpleNamespace(text="2", value="opt_a")],
            state={"n": 1},
        )
        client = SimpleNamespace(execute_ask=AsyncMock(return_value=ask_result))
        monkeypatch.setattr("src.services.sandbox_client.get_sandbox_client", lambda: client)
        monkeypatch.setattr(
            "src.repositories.custom_verification_repo.CustomVerificationRepository.get_active_revision",
            AsyncMock(
                return_value=SimpleNamespace(
                    id=7, language="python", source="S", source_sha256="a" * 64
                )
            ),
        )
        group = SimpleNamespace(verification_timeout=120)
        prepared = await VerificationService.prepare_script_challenge(
            group,
            CHAT,
            USER,
            session_id=SESSION,
            locale="zh-Hans",
            username="测试",
        )
        assert isinstance(prepared.challenge, ScriptChallenge)
        assert prepared.challenge.text == "1+1=?"
        assert prepared.challenge.options[0].value == "opt_a"
        assert prepared.state_value.startswith("script:")
        assert ":" not in prepared.state_value[len("script:") :]
        # 会话绑定负载含 verify 重建 ctx 所需的元数据
        payload = json.loads(prepared.script_state)
        assert payload["revision_id"] == 7
        assert payload["option_values"] == ["opt_a"]
        assert payload["locale"] == "zh-Hans"
        assert payload["username"] == "测试"
        # ask ctx 契约字段
        ctx = client.execute_ask.await_args.args[2]
        assert ctx["api_version"] == 1
        assert ctx["user"]["first_name"] == "测试"
        assert ctx["state"] is None

    async def test_token_is_stable_and_session_bound(self):
        token_a = VerificationService._script_state_token("s1", 7, {"a": 1}, ["x"])
        assert token_a == VerificationService._script_state_token("s1", 7, {"a": 1}, ["x"])
        # session 纳入哈希：不同会话即使题目内容完全相同也必得不同 token
        assert token_a != VerificationService._script_state_token("s2", 7, {"a": 1}, ["x"])
        assert token_a != VerificationService._script_state_token("s1", 8, {"a": 1}, ["x"])


class TestCommitChallengeScriptState:
    """commit_challenge 提交成功后必须写脚本状态键（恢复链路依赖此行为，F1 回归）。"""

    def _env(self, monkeypatch: pytest.MonkeyPatch, *, committed: bool, script_state: str | None):
        # 只覆盖 commit_challenge 实际访问的字段；cast 满足静态类型收窄
        reservation = cast(
            "RecoveryReservation",
            SimpleNamespace(chat_id=CHAT, user_id=USER, session_id=SESSION, deadline_ms=2000),
        )
        commit_recovery = AsyncMock(return_value=committed)
        redis = SimpleNamespace(set=AsyncMock())
        monkeypatch.setattr(service, "commit_recovery", commit_recovery)
        monkeypatch.setattr(service, "get_redis", lambda: redis)
        prepared = cast(
            "PreparedChallenge",
            SimpleNamespace(
                state_value="script:tok", auxiliary_state=None, script_state=script_state
            ),
        )
        return redis, reservation, prepared

    async def test_commit_writes_script_state_with_pxat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """脚本题提交成功 → script_state 键按 deadline+grace 的 PXAT 写入。"""
        redis, reservation, prepared = self._env(
            monkeypatch, committed=True, script_state='{"r":1}'
        )

        ok = await VerificationService.commit_challenge(
            CHAT, USER, prepared, SESSION, 2000, "join", reservation=reservation
        )

        assert ok is True
        redis.set.assert_awaited_once_with(
            RedisKeys.verification_script_state(CHAT, USER),
            '{"r":1}',
            pxat=2000 + service.VERIFICATION_GRACE_MS,
        )

    async def test_commit_failure_skips_script_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """主键 CAS 失败 → 绝不写脚本状态键（避免为未提交的主键留残键）。"""
        redis, reservation, prepared = self._env(
            monkeypatch, committed=False, script_state='{"r":1}'
        )

        ok = await VerificationService.commit_challenge(
            CHAT, USER, prepared, SESSION, 2000, "join", reservation=reservation
        )

        assert ok is False
        redis.set.assert_not_awaited()

    async def test_non_script_challenge_never_writes_script_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非脚本题（script_state=None）→ 不写脚本状态键，行为与旧 commit_recovery 等价。"""
        redis, reservation, prepared = self._env(monkeypatch, committed=True, script_state=None)

        ok = await VerificationService.commit_challenge(
            CHAT, USER, prepared, SESSION, 2000, "join", reservation=reservation
        )

        assert ok is True
        redis.set.assert_not_awaited()


class TestScriptStateKey:
    def test_key_format(self):
        assert RedisKeys.verification_script_state(CHAT, USER) == (
            f"verification_script_state:{CHAT}:{USER}"
        )
