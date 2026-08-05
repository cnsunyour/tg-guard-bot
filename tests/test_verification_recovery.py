"""验证 delivery/recovery 状态机测试。

测试策略：_FakeRedis 模拟 redis-py 调用契约 + 7 个 Lua 脚本语义（含 Redis TIME），验证
Python 侧状态机逻辑。不等同于真实 Redis Lua 集成测试（真机验证在 3c1-4b 接入后）。

覆盖 4a 的四个证明点：
- 不重复发送（并发 initial reserve 只一个赢）
- 不复活已清理状态（clear 后 commit 失败）
- 旧 timeout 不处理新 session（claim 匹配 session）
- message_id 关联（promote 写真实 id，claim 读真实 id）
外加 Forbidden 保留 undelivered、WebApp auxiliary 同事务提交。
"""

import asyncio

import pytest

from src.core.redis import RedisKeys
from src.services import verification
from src.services import verification_recovery as recovery
from src.services.verification import MathChallenge, PreparedChallenge, VerificationService
from src.services.verification_recovery import (
    VerificationFlow,
    claim_success,
    claim_timeout,
    commit_recovery,
    promote_recovery,
    release_recovery,
    reserve_initial_recovery,
)

pytestmark = pytest.mark.unit

CHAT_ID = -100
USER_ID = 42


class _FakeRedis:
    """模拟 redis-py 调用契约 + verification_recovery 的 7 个 Lua 脚本语义。

    ``now_ms`` 模拟 Redis ``TIME``（毫秒），测试通过推进它验证 deadline 逻辑。
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.now_ms = 1_000_000

    async def get(self, key):
        return self.values.get(key)

    async def mget(self, *keys):
        return [self.values.get(key) for key in keys]

    async def exists(self, key):
        return int(key in self.values)

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.values:
                deleted += 1
                del self.values[key]
        return deleted

    async def eval(self, script, numkeys, *parts):
        keys = parts[:numkeys]
        args = parts[numkeys:]

        if script == recovery._RESERVE_RECOVERY_SCRIPT:
            return self._eval_reserve(keys, args)
        if script == recovery._COMMIT_RECOVERY_SCRIPT:
            return self._eval_commit(keys, args)
        if script == recovery._PROMOTE_RECOVERY_SCRIPT:
            return self._eval_promote(keys, args)
        if script == recovery._RELEASE_RECOVERY_SCRIPT:
            return self._eval_release(keys, args)
        if script == recovery._CLAIM_SUCCESS_SCRIPT:
            return self._eval_claim_success(keys, args)
        if script == recovery._CLEAR_VERIFICATION_SCRIPT:
            return self._eval_clear(keys, args)
        if script == recovery._CLAIM_TIMEOUT_SCRIPT:
            return self._eval_claim(keys, args)
        raise AssertionError("unexpected Lua script")

    def _eval_reserve(self, keys, args):
        recovery_key, main_key, deadline_key = keys
        mode, requested_session, revision, owner = args[:4]
        timeout_ms = int(args[4])

        if mode == "initial":
            if any(k in self.values for k in (recovery_key, main_key, deadline_key)):
                return [0, "busy", ""]
            session = requested_session
            deadline_ms = self.now_ms + timeout_ms
            expected_main = ""
        else:
            deadline_raw = self.values.get(deadline_key)
            expected_main = self.values.get(main_key)
            if not deadline_raw or expected_main is None:
                return [0, "missing", ""]
            session, deadline_text = deadline_raw.split(":", 1)
            deadline_ms = int(deadline_text)
            if deadline_ms <= self.now_ms:
                return [0, "expired", ""]
            current = self.values.get(recovery_key)
            if current not in (None, f"undelivered:{session}"):
                return [0, "busy", ""]

        self.values[recovery_key] = f"pending:{session}:{revision}:{owner}"
        return [1, str(deadline_ms), expected_main, session]

    def _eval_commit(self, keys, args):
        recovery_key, main_key, deadline_key, type_key, token_key = keys
        (
            expected_pending,
            mode,
            expected_main,
            session,
            deadline_text,
            state_value,
            flow,
            auxiliary,
            _grace,
        ) = args
        deadline_ms = int(deadline_text)

        if self.now_ms >= deadline_ms:
            return 0
        if self.values.get(recovery_key) != expected_pending:
            return 0

        expected_deadline = f"{session}:{deadline_ms}"
        if mode == "initial":
            if main_key in self.values or deadline_key in self.values:
                return 0
        elif (
            self.values.get(main_key) != expected_main
            or self.values.get(deadline_key) != expected_deadline
            or self.values.get(type_key) != flow
        ):
            return 0

        self.values[main_key] = state_value
        self.values[deadline_key] = expected_deadline
        self.values[type_key] = flow
        if auxiliary:
            self.values[token_key] = auxiliary
        else:
            self.values.pop(token_key, None)
        return 1

    def _eval_promote(self, keys, args):
        recovery_key, deadline_key, main_key = keys
        expected_pending, expected_deadline, message_value = args[:3]
        deadline_ms = int(args[3])
        if (
            self.now_ms >= deadline_ms
            or self.values.get(recovery_key) != expected_pending
            or self.values.get(deadline_key) != expected_deadline
            or main_key not in self.values
        ):
            return 0
        self.values[recovery_key] = message_value
        return 1

    def _eval_release(self, keys, args):
        recovery_key = keys[0]
        expected_pending = args[0]
        if self.values.get(recovery_key) != expected_pending:
            return 0
        if args[5] == "0":
            # preserve_challenge=False：删全部状态键
            for key in keys:
                self.values.pop(key, None)
        else:
            # preserve_challenge=True：pending → undelivered（校验 main 在 + deadline 匹配）
            main_key, deadline_key = keys[1], keys[2]
            expected_deadline = args[2]
            if main_key not in self.values or self.values.get(deadline_key) != expected_deadline:
                return 0
            self.values[recovery_key] = args[1]
        return 1

    def _eval_claim_success(self, keys, args):
        recovery_key, main_key, deadline_key, type_key, token_key = keys
        expected_main, expected_deadline, grace_text = args

        # deadline CAS（整体字符串相等，防 session 切换 ABA）+ 格式校验
        deadline_raw = self.values.get(deadline_key)
        if deadline_raw is None or deadline_raw != expected_deadline:
            return 0
        session, sep, deadline_text = deadline_raw.partition(":")
        if not sep or not session or not deadline_text.isdigit():
            return 0
        deadline_ms = int(deadline_text)

        # grace 期保护（_FakeRedis 无 TTL，需显式判断）
        if self.now_ms >= deadline_ms + int(grace_text):
            return 0

        # main CAS + flow 白名单
        if self.values.get(main_key) != expected_main:
            return 0
        if self.values.get(type_key) not in ("join", "join_request"):
            return 0

        self.values.pop(recovery_key, None)
        self.values.pop(main_key, None)
        self.values.pop(deadline_key, None)
        self.values.pop(token_key, None)
        # type 留给成功 handler 读取 flow 后删除
        return 1

    def _eval_clear(self, keys, args):
        recovery_key, main_key, deadline_key, _type_key, _token_key = keys
        (
            state_present,
            expected_state,
            deadline_present,
            expected_deadline,
            recovery_present,
            expected_recovery,
        ) = args

        current_state = self.values.get(main_key)
        if (state_present == "1" and current_state != expected_state) or (
            state_present == "0" and current_state is not None
        ):
            return 0

        current_deadline = self.values.get(deadline_key)
        if (deadline_present == "1" and current_deadline != expected_deadline) or (
            deadline_present == "0" and current_deadline is not None
        ):
            return 0

        # nil deadline 时校验 recovery（reservation 身份）
        if deadline_present == "0":
            current_recovery = self.values.get(recovery_key)
            if (recovery_present == "1" and current_recovery != expected_recovery) or (
                recovery_present == "0" and current_recovery is not None
            ):
                return 0

        deleted = 0
        for key in keys:
            if self.values.pop(key, None) is not None:
                deleted += 1
        return deleted

    def _eval_claim(self, keys, args):
        recovery_key, main_key, deadline_key, type_key, token_key = keys
        session, flow = args[:2]
        if flow not in ("join", "join_request"):
            return [0, 0, 0]
        deadline_raw = self.values.get(deadline_key)
        if (
            not deadline_raw
            or not deadline_raw.startswith(f"{session}:")
            or self.values.get(type_key) != flow
            or main_key not in self.values
        ):
            return [0, 0, 0]

        deadline_ms = int(deadline_raw.split(":", 1)[1])
        if self.now_ms < deadline_ms:
            return [1, 0, deadline_ms - self.now_ms]

        raw = self.values.get(recovery_key)
        if raw == f"timeout:{session}":
            return [0, 0, 0]

        # 严格字段校验（与 _CLAIM_TIMEOUT_SCRIPT 对称：pending 4 段 / message 5 段 + flow 一致）
        message_id = 0
        if raw is not None:
            fields = raw.split(":")
            if raw == f"undelivered:{session}" or (
                len(fields) == 4
                and fields[0] == "pending"
                and fields[1] == session
                and fields[2]
                and fields[3]
            ):
                pass  # message_id 保持 0
            elif (
                len(fields) == 5
                and fields[0] == "message"
                and fields[1] == session
                and fields[2]
                and fields[3] == flow
                and fields[4].isdigit()
            ):
                message_id = int(fields[4])
            else:
                return [0, 0, 0]

        timeout_value = f"timeout:{session}"
        self.values.pop(main_key, None)
        self.values.pop(token_key, None)
        self.values[recovery_key] = timeout_value
        return [2, message_id, 0, deadline_raw, timeout_value]


def _patch_redis(mocker, redis: _FakeRedis) -> None:
    mocker.patch.object(recovery, "get_redis", return_value=redis)
    mocker.patch.object(verification, "get_redis", return_value=redis)


def _prepared(state_value: str = "math:4", auxiliary: str | None = None) -> PreparedChallenge:
    return PreparedChallenge(
        challenge=MathChallenge(expression="2 + 2", choices=(3, 4, 5, 6)),
        state_value=state_value,
        auxiliary_state=auxiliary,
    )


async def _create_committed(redis: _FakeRedis, session_id: str, *, flow: VerificationFlow = "join"):
    """reserve_initial + commit，返回 reservation（供后续 promote/release/claim 测试）。"""
    reservation = await reserve_initial_recovery(CHAT_ID, USER_ID, session_id, timeout_ms=120_000)
    assert reservation is not None
    assert await commit_recovery(reservation, state_value="math:4", auxiliary_state=None, flow=flow)
    return reservation


async def test_concurrent_initial_reserve_has_one_winner(mocker) -> None:
    """不重复发送：并发 initial reserve 只有一个能赢（NX 语义）。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    first, second = await asyncio.gather(
        reserve_initial_recovery(CHAT_ID, USER_ID, "session-a", 120_000),
        reserve_initial_recovery(CHAT_ID, USER_ID, "session-b", 120_000),
    )

    assert sum(item is not None for item in (first, second)) == 1


async def test_commit_does_not_resurrect_cleared_state(mocker) -> None:
    """不复活已清理状态：clear 后 commit CAS 失败，主键不被写回。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await reserve_initial_recovery(CHAT_ID, USER_ID, "session-a", timeout_ms=120_000)
    assert reservation is not None

    await VerificationService.clear_verification(CHAT_ID, USER_ID)

    assert not await VerificationService.commit_challenge(
        CHAT_ID,
        USER_ID,
        _prepared(),
        reservation.session_id,
        reservation.deadline_ms,
        "join",
        reservation=reservation,
    )
    assert RedisKeys.verification(CHAT_ID, USER_ID) not in redis.values


async def test_stale_clear_does_not_delete_new_session(mocker) -> None:
    """旧协程持有的 clear token 不能删除 grace 后建立的新 session（P2.1 核心）。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    await _create_committed(redis, "old-session")
    old_token = await VerificationService.capture_clear_token(CHAT_ID, USER_ID)

    # 模拟旧状态被正常清除后，新 session 已完整建立
    assert await VerificationService.clear_verification(CHAT_ID, USER_ID, expected=old_token)
    redis.now_ms += 1_000
    new = await _create_committed(redis, "new-session")
    token_key = RedisKeys.captcha_token(CHAT_ID, USER_ID)
    redis.values[token_key] = "hcaptcha:new-token"

    # 旧协程持旧 token 再清理：CAS 失败（deadline 换新 session），不删新 session
    assert not await VerificationService.clear_verification(CHAT_ID, USER_ID, expected=old_token)
    assert redis.values[RedisKeys.verification(CHAT_ID, USER_ID)] == "math:4"
    assert (
        redis.values[RedisKeys.verification_deadline(CHAT_ID, USER_ID)]
        == f"new-session:{new.deadline_ms}"
    )
    assert redis.values[RedisKeys.verification_type(CHAT_ID, USER_ID)] == "join"
    assert redis.values[token_key] == "hcaptcha:new-token"


async def test_nil_deadline_clear_does_not_delete_new_reservation(mocker) -> None:
    """nil deadline 快照也不能删除随后写入的 initial pending reservation。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    empty_token = await VerificationService.capture_clear_token(CHAT_ID, USER_ID)
    reservation = await reserve_initial_recovery(
        CHAT_ID, USER_ID, "new-session", timeout_ms=120_000
    )
    assert reservation is not None

    assert not await VerificationService.clear_verification(CHAT_ID, USER_ID, expected=empty_token)
    assert (
        redis.values[RedisKeys.verification_recovery(CHAT_ID, USER_ID)] == reservation.pending_value
    )


async def test_old_timeout_cannot_claim_new_session(mocker) -> None:
    """旧 timeout 不处理新 session：旧 session 的 timeout claim 返回 stale，不误罚新会话。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    await _create_committed(redis, "old-session")
    await VerificationService.clear_verification(CHAT_ID, USER_ID)

    redis.now_ms += 1_000
    new = await _create_committed(redis, "new-session")
    redis.now_ms = new.deadline_ms + 1

    old_claim = await claim_timeout(CHAT_ID, USER_ID, "old-session", "join")

    assert old_claim.status == "stale"
    assert (
        redis.values[RedisKeys.verification_deadline(CHAT_ID, USER_ID)]
        == f"new-session:{new.deadline_ms}"
    )


async def test_promote_associates_real_message_id_for_timeout(mocker) -> None:
    """message_id 关联：promote 写真实 message_id，timeout claim 据此读取（解决 message_id=0）。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await _create_committed(redis, "session-a")
    assert await promote_recovery(reservation, "join", message_id=9876)

    redis.now_ms = reservation.deadline_ms
    claim = await claim_timeout(CHAT_ID, USER_ID, "session-a", "join")

    assert claim.status == "claimed"
    assert claim.message_id == 9876


@pytest.mark.parametrize(
    "recovery_value",
    [
        "pending:session-a:",  # 缺 revision/owner
        "pending:session-a:a:b:c:d",  # 多字段
        "message:session-a:revision:not_flow:123",  # flow 非法
        "message:session-a:revision:join:not-a-number",  # message_id 非数字
        "message:session-a:revision:join:123:extra",  # 多字段
        "message:session-a:revision:join_request:123",  # flow 与本次 claim (join) 不一致
    ],
)
async def test_claim_timeout_rejects_invalid_recovery(mocker, recovery_value: str) -> None:
    """损坏或 flow 不一致的 recovery 必须 fail closed（stale），不能消费当前状态。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await _create_committed(redis, "session-a")
    recovery_key = RedisKeys.verification_recovery(CHAT_ID, USER_ID)
    main_key = RedisKeys.verification(CHAT_ID, USER_ID)
    redis.values[recovery_key] = recovery_value
    redis.now_ms = reservation.deadline_ms

    claim = await claim_timeout(CHAT_ID, USER_ID, "session-a", "join")

    assert claim.status == "stale"
    assert redis.values[main_key] == "math:4"  # 未被消费
    assert redis.values[recovery_key] == recovery_value


async def test_forbidden_release_preserves_undelivered_state(mocker) -> None:
    """Forbidden 后保留 undelivered：主键/deadline/type 仍在，recovery=undelivered 供 /start 恢复。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await _create_committed(redis, "session-a")
    assert await release_recovery(reservation, preserve_challenge=True)

    assert (
        redis.values[RedisKeys.verification_recovery(CHAT_ID, USER_ID)] == "undelivered:session-a"
    )
    assert redis.values[RedisKeys.verification(CHAT_ID, USER_ID)] == "math:4"
    assert (
        redis.values[RedisKeys.verification_deadline(CHAT_ID, USER_ID)]
        == f"session-a:{reservation.deadline_ms}"
    )


async def test_webapp_auxiliary_is_committed_with_main(mocker) -> None:
    """WebApp auxiliary 同事务提交：captcha_token 与主键在同一 commit Lua 内写入。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await reserve_initial_recovery(
        CHAT_ID, USER_ID, "session-web", timeout_ms=120_000
    )
    assert reservation is not None

    assert await commit_recovery(
        reservation,
        state_value="hcaptcha:pending",
        auxiliary_state="hcaptcha:secret-token",
        flow="join",
    )

    assert redis.values[RedisKeys.verification(CHAT_ID, USER_ID)] == "hcaptcha:pending"
    assert redis.values[RedisKeys.captcha_token(CHAT_ID, USER_ID)] == "hcaptcha:secret-token"


async def test_success_claim_consumes_state_and_makes_timeout_stale(mocker) -> None:
    """成功先 claim：消费 main/deadline/token，timeout 随后只能返回 stale。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await _create_committed(redis, "session-a")
    main_key = RedisKeys.verification(CHAT_ID, USER_ID)
    deadline_key = RedisKeys.verification_deadline(CHAT_ID, USER_ID)
    type_key = RedisKeys.verification_type(CHAT_ID, USER_ID)
    token_key = RedisKeys.captcha_token(CHAT_ID, USER_ID)
    redis.values[token_key] = "captcha:token"

    claimed = await claim_success(
        CHAT_ID,
        USER_ID,
        redis.values[main_key],
        redis.values[deadline_key],
    )

    assert claimed
    assert RedisKeys.verification_recovery(CHAT_ID, USER_ID) not in redis.values
    assert main_key not in redis.values
    assert deadline_key not in redis.values
    assert token_key not in redis.values
    assert redis.values[type_key] == "join"  # type 留给 handler 读 flow

    redis.now_ms = reservation.deadline_ms
    assert (await claim_timeout(CHAT_ID, USER_ID, "session-a", "join")).status == "stale"


async def test_success_claim_allows_subsequent_reserve(mocker) -> None:
    """成功 claim 删 recovery 终态，后续新 session reserve 不被阻塞（防重入卡死）。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    await _create_committed(redis, "session-a")
    main_key = RedisKeys.verification(CHAT_ID, USER_ID)
    deadline_key = RedisKeys.verification_deadline(CHAT_ID, USER_ID)

    assert await claim_success(CHAT_ID, USER_ID, redis.values[main_key], redis.values[deadline_key])

    # recovery/main/deadline 已删（type 残留但 reserve 不查）→ 新 session 可 reserve
    redis.now_ms += 1_000
    new_reservation = await reserve_initial_recovery(CHAT_ID, USER_ID, "session-b", 120_000)
    assert new_reservation is not None


async def test_timeout_claim_wins_before_success_claim(mocker) -> None:
    """timeout 先 claim：成功 CAS 失败，调用方必须返回 expired。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await _create_committed(redis, "session-a")
    expected_main = redis.values[RedisKeys.verification(CHAT_ID, USER_ID)]
    expected_deadline = redis.values[RedisKeys.verification_deadline(CHAT_ID, USER_ID)]
    redis.now_ms = reservation.deadline_ms

    assert (await claim_timeout(CHAT_ID, USER_ID, "session-a", "join")).status == "claimed"
    assert not await claim_success(CHAT_ID, USER_ID, expected_main, expected_deadline)


async def test_success_claim_rejects_session_switch_even_when_main_is_same(mocker) -> None:
    """deadline CAS 防 ABA：新 session 生成相同答案时，旧快照不能消费新状态。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    await _create_committed(redis, "old-session")
    expected_main = redis.values[RedisKeys.verification(CHAT_ID, USER_ID)]
    expected_deadline = redis.values[RedisKeys.verification_deadline(CHAT_ID, USER_ID)]

    await VerificationService.clear_verification(CHAT_ID, USER_ID)
    redis.now_ms += 1_000
    new_reservation = await _create_committed(redis, "new-session")

    assert not await claim_success(CHAT_ID, USER_ID, expected_main, expected_deadline)
    # 新会话状态未被旧快照消费
    assert redis.values[RedisKeys.verification(CHAT_ID, USER_ID)] == expected_main
    assert (
        redis.values[RedisKeys.verification_deadline(CHAT_ID, USER_ID)]
        == f"new-session:{new_reservation.deadline_ms}"
    )


async def test_success_claim_rejects_malformed_deadline(mocker) -> None:
    """deadline 损坏时 fail closed，不消费 main 或覆盖 recovery。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    await _create_committed(redis, "session-a")
    main_key = RedisKeys.verification(CHAT_ID, USER_ID)
    deadline_key = RedisKeys.verification_deadline(CHAT_ID, USER_ID)
    redis.values[deadline_key] = "session-a:not-a-number"

    assert not await claim_success(
        CHAT_ID, USER_ID, redis.values[main_key], redis.values[deadline_key]
    )
    assert main_key in redis.values  # 未被消费


async def test_claim_deletes_main_to_mutual_exclude_success_callback(mocker) -> None:
    """claim 即消费：claim 删主键 + captcha_token，使 timeout 与用户成功回调互斥。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await _create_committed(redis, "session-a")
    await promote_recovery(reservation, "join", message_id=555)

    redis.now_ms = reservation.deadline_ms
    claim = await claim_timeout(CHAT_ID, USER_ID, "session-a", "join")

    assert claim.status == "claimed"
    # claim 删了主键（verify_answer 后续读不到）+ recovery 置 timeout（防二次 claim）
    assert RedisKeys.verification(CHAT_ID, USER_ID) not in redis.values
    assert redis.values[RedisKeys.verification_recovery(CHAT_ID, USER_ID)] == "timeout:session-a"


async def test_second_claim_after_timeout_is_stale(mocker) -> None:
    """防重复处罚：claim 后 recovery=timeout，二次 claim 返回 stale。"""
    redis = _FakeRedis()
    _patch_redis(mocker, redis)

    reservation = await _create_committed(redis, "session-a")
    redis.now_ms = reservation.deadline_ms

    first = await claim_timeout(CHAT_ID, USER_ID, "session-a", "join")
    second = await claim_timeout(CHAT_ID, USER_ID, "session-a", "join")

    assert first.status == "claimed"
    assert second.status == "stale"
