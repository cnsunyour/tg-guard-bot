"""群成员集体投票回调与终态裁决。

成员对待确认消息（自动检测确认模式 / 举报）投「垃圾 +1」或「误报 -1」：按钮与
/spam、/unspam 指令两条入口共用本模块的资格校验与终态逻辑。单向票数达到会话
``threshold`` 后，在 ``review_lock``（同消息处置互斥）内原子消费会话并执行与
管理员确认一致的处置——at-most-once：消费成功者执行，失败者放弃。

并发要点见 ``src/services/spam_vote.py`` 模块注释；callback 只 answer 一次
（投票结果 toast 在 cast_vote 之后发出，之后的 finalize 不再 answer）。
"""

from __future__ import annotations

import contextlib
import re
from typing import Literal, cast

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message
from loguru import logger

from src.bot.handlers.antispam_render import (
    build_report_keyboard,
    build_review_keyboard,
    build_vote_ham_result,
    build_vote_progress,
    build_vote_row,
    build_vote_spam_result,
)
from src.core.cache import PermissionCache
from src.core.config import settings
from src.core.i18n import BoundLocalizer, get_resolver, get_translator
from src.core.utils import auto_delete_message, escape_html
from src.repositories.audit_repo import AuditRepository
from src.repositories.group_repo import GroupRepository
from src.repositories.report_repo import ReportRepository
from src.services.moderation import ModerationService
from src.services.spam_detector import get_detector
from src.services.spam_review import (
    consume_review_state,
    get_review_state,
    review_lock,
)
from src.services.spam_vote import (
    SpamVoteSession,
    VoteOutcome,
    cast_vote,
    consume_vote_session,
    get_vote_prompt,
    get_vote_session,
)

router = Router(name="spam_vote")

_VOTE_ID_PATTERN = re.compile(r"[0-9a-fA-F]{16}")

# GroupAnonymousBot 固定 ID：匿名管理员点击时 callback 无 sender_chat，按此识别，
# 与管理员直达按钮同权限模型（不参与投票）
_ANONYMOUS_ADMIN_BOT_ID = 1087968824

# 资格校验结果 → 各入口对应的提示 key 后缀（toast / message）
Eligibility = Literal["ok", "disabled", "admin", "offender"]
_ELIGIBILITY_TOAST_KEYS: dict[str, str] = {
    "disabled": "spam_vote.callback.disabled.toast",
    "admin": "spam_vote.callback.admin_direct.toast",
    "offender": "spam_vote.callback.offender.toast",
}
_ELIGIBILITY_MESSAGE_KEYS: dict[str, str] = {
    "disabled": "spam_vote.command.disabled.message",
    "admin": "spam_vote.command.admin_direct.message",
    "offender": "spam_vote.command.offender.message",
}


async def _answer_toast(
    callback: CallbackQuery,
    key: str,
    *,
    show_alert: bool = True,
    **variables: object,
) -> None:
    """按点击者个人 locale 应答 toast（与 antispam._answer_toast 同范式）。"""
    locale = await get_resolver().for_user(callback.from_user.id)
    await callback.answer(
        get_translator().for_locale(locale).t(key, **variables),
        show_alert=show_alert,
    )


async def vote_enabled(chat_id: int) -> bool:
    """群级集体投票开关；群配置缺失（未入库）时视为默认开启。"""
    group = await GroupRepository.get(chat_id)
    return group is None or bool(group.spam_vote_enabled)


async def is_vote_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """管理员判定：超级管理员直通 / 匿名管理员 / 群管理员（Redis 缓存）。"""
    if user_id in settings.admin_ids or user_id == _ANONYMOUS_ADMIN_BOT_ID:
        return True
    return await PermissionCache.is_admin(bot, chat_id, user_id)


async def check_vote_eligibility(
    bot: Bot,
    chat_id: int,
    user_id: int,
    session: SpamVoteSession,
) -> Eligibility:
    """投票资格：开关开启 + 非管理员 + 非被举报者本人。

    管理员不参与投票（同样的点操作/发指令对管理员已是直达处理）；offender
    自投反对票存在直接利益冲突，一并排除。
    """
    if not await vote_enabled(chat_id):
        return "disabled"
    if await is_vote_admin(bot, chat_id, user_id):
        return "admin"
    if session.offender_user_id == user_id:
        return "offender"
    return "ok"


async def _rebuild_prompt_keyboard(
    localizer: BoundLocalizer,
    chat_id: int,
    orig_msg_id: int,
    session: SpamVoteSession,
) -> InlineKeyboardMarkup:
    """重建提示消息键盘：投票行置顶 + 按来源链路保留管理员直达按钮。

    review 链路的管理员按钮携带 review_id（快照身份），需实时查 review state；
    state 已过期时仅剩投票行（管理员按钮本就随 state 失效）。
    """
    vote_row = build_vote_row(localizer, orig_msg_id, session.vote_id)
    if session.source.value == "report" and session.report_id is not None:
        return build_report_keyboard(localizer, session.report_id, vote_row=vote_row)

    review_state = await get_review_state(chat_id, orig_msg_id)
    if review_state is None:
        return InlineKeyboardMarkup(inline_keyboard=[vote_row])
    return build_review_keyboard(localizer, orig_msg_id, review_state.review_id, vote_row=vote_row)


async def edit_vote_progress(
    bot: Bot,
    chat_id: int,
    orig_msg_id: int,
    up: int,
    down: int,
    threshold: int,
) -> None:
    """以最新票数更新提示消息（best-effort：任何失败仅记日志，不影响投票已生效）。

    edit 前重查会话：已被消费（终局已出）则不再发进度编辑，防止进度行覆盖结果段。
    """
    session = await get_vote_session(chat_id, orig_msg_id)
    prompt = await get_vote_prompt(chat_id, orig_msg_id)
    if session is None or prompt is None:
        return
    prompt_message_id, prompt_base = prompt

    group_locale = await get_resolver().for_group(chat_id)
    localizer = get_translator().for_locale(group_locale)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=prompt_message_id,
            text=f"{prompt_base}\n\n{build_vote_progress(localizer, up, down, threshold)}",
            reply_markup=await _rebuild_prompt_keyboard(localizer, chat_id, orig_msg_id, session),
            disable_web_page_preview=True,
        )
    except Exception as e:
        # 并发进度编辑可能交错（Telegram 按到达序应用），下一次投票自愈
        logger.debug(f"投票进度编辑失败 [群组:{chat_id}] [消息:{orig_msg_id}]: {e}")


async def _finalize_vote_outcome(
    bot: Bot,
    chat_id: int,
    orig_msg_id: int,
    session: SpamVoteSession,
    operator_id: int,
    verdict: Literal["spam", "ham"],
    up: int,
    down: int,
) -> str:
    """执行终态处置（调用前会话已被本协程原子消费），返回结果文案。

    ``verdict`` 由触发方向决定（以本票方向优先），不按票数重新判断——双向
    同时达标时以触发者的方向为准。各业务步骤独立 suppress：互不阻断，结果
    文案始终返回（对齐 review callback）。
    """
    group_locale = await get_resolver().for_group(chat_id)
    localizer = get_translator().for_locale(group_locale)

    if verdict == "spam":
        result = await ModerationService.ban_user(
            bot=bot,
            chat_id=chat_id,
            user_id=session.offender_user_id,
            operator_id=operator_id,
            reason="垃圾信息（集体投票确认）",
            revoke_messages=False,
            allow_left=True,
        )
        if result.success:
            with contextlib.suppress(Exception):
                await get_detector().add_feedback(
                    text=session.sample_text,
                    is_spam=True,
                    labeled_by=operator_id,
                )
            with contextlib.suppress(Exception):
                await AuditRepository.log_action(
                    group_id=chat_id,
                    operator_id=operator_id,
                    action="spam_vote_ban",
                    target_user_id=session.offender_user_id,
                    details={
                        "orig_msg_id": orig_msg_id,
                        "up": up,
                        "down": down,
                        "threshold": session.threshold,
                    },
                )
            with contextlib.suppress(Exception):
                await ReportRepository.update_reports_status_by_message(
                    group_id=chat_id,
                    message_id=orig_msg_id,
                    status="approved",
                    handled_by=operator_id,
                )
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id, orig_msg_id)
            logger.info(
                f"集体投票确认垃圾 [群组:{chat_id}] [用户:{session.offender_user_id}] "
                f"[触发者:{operator_id}] 票数:{up}/{session.threshold}"
            )
            return build_vote_spam_result(localizer, up, session.threshold)

        # 处罚失败（FloodWait / API 故障等）：审计失败原因并展示错误，不保留按钮重试
        assert result.code is not None
        with contextlib.suppress(Exception):
            await AuditRepository.log_action(
                group_id=chat_id,
                operator_id=operator_id,
                action="spam_vote_ban_failed",
                target_user_id=session.offender_user_id,
                details={
                    "orig_msg_id": orig_msg_id,
                    "up": up,
                    "down": down,
                    "threshold": session.threshold,
                    "error_code": result.code.value,
                },
            )
        logger.warning(
            f"集体投票处罚失败 [群组:{chat_id}] [用户:{session.offender_user_id}] "
            f"[错误:{result.code.value}]"
        )
        return localizer.t(
            "spam_vote.action_failed.message",
            error=escape_html(localizer.t(f"moderation.error.{result.code.value}.message")),
        )

    # 误报达阈（调用方已保证 up >= threshold 或 down >= threshold 二者其一）
    with contextlib.suppress(Exception):
        await get_detector().add_feedback(
            text=session.sample_text,
            is_spam=False,
            labeled_by=operator_id,
        )
    with contextlib.suppress(Exception):
        await AuditRepository.log_action(
            group_id=chat_id,
            operator_id=operator_id,
            action="spam_vote_false_positive",
            target_user_id=session.offender_user_id,
            details={
                "orig_msg_id": orig_msg_id,
                "up": up,
                "down": down,
                "threshold": session.threshold,
            },
        )
    with contextlib.suppress(Exception):
        await ReportRepository.update_reports_status_by_message(
            group_id=chat_id,
            message_id=orig_msg_id,
            status="rejected",
            handled_by=operator_id,
        )
    logger.info(
        f"集体投票确认误报 [群组:{chat_id}] [用户:{session.offender_user_id}] "
        f"[触发者:{operator_id}] 票数:{down}/{session.threshold}"
    )
    return build_vote_ham_result(localizer, down, session.threshold)


async def finalize_vote_if_ready(
    bot: Bot,
    chat_id: int,
    orig_msg_id: int,
    session: SpamVoteSession,
    operator_id: int,
    direction: Literal["up", "down"],
    up: int,
    down: int,
    *,
    prompt_message: Message | None = None,
) -> bool:
    """本票方向达到阈值时执行终局裁决；返回是否已达阈值并进入处理流程。

    互斥与 at-most-once 保证：``review_lock``（同消息处置锁，与管理员直达路径
    共用）内先 ``consume_vote_session``——多成员同时投到阈值 / 与管理员并发处理
    时，仅消费成功者继续执行，其余直接返回。会话消费后再 best-effort 消费
    review state（不按来源判断：举报先行、检测后命中的交错下，管理员旧按钮
    也必须失效）。

    ``prompt_message``（按钮路径的 callback.message）提供时结果编辑后 30s 自删；
    指令路径不提供——提示消息仍由创建时的 1h auto_delete 任务收尾。
    """
    reached = up >= session.threshold if direction == "up" else down >= session.threshold
    if not reached:
        return False

    async with review_lock(chat_id, orig_msg_id) as acquired:
        if not acquired:
            return True  # 管理员或另一投票终局正在处理，本协程放弃

        consumed = await consume_vote_session(chat_id, orig_msg_id, session.vote_id)
        if consumed is None:
            return True  # 会话已被他方消费（或已过期重建），本协程放弃

        with contextlib.suppress(Exception):
            review_state = await get_review_state(chat_id, orig_msg_id)
            if review_state is not None:
                await consume_review_state(chat_id, orig_msg_id, review_state.review_id)

        result_text = await _finalize_vote_outcome(
            bot,
            chat_id,
            orig_msg_id,
            consumed,
            operator_id,
            cast("Literal['spam', 'ham']", "spam" if direction == "up" else "ham"),
            up,
            down,
        )

    prompt = await get_vote_prompt(chat_id, orig_msg_id)
    if prompt is not None:
        prompt_message_id, prompt_base = prompt
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=prompt_message_id,
                text=f"{prompt_base}\n\n{result_text}",
                reply_markup=None,
                disable_web_page_preview=True,
            )
    if prompt_message is not None:
        with contextlib.suppress(Exception):
            await auto_delete_message(prompt_message, delay=30)
    return True


async def handle_vote_command(
    bot: Bot,
    message: Message,
    localizer: BoundLocalizer,
    target_message_id: int,
    direction: Literal["up", "down"],
) -> None:
    """/spam（已有会话时）与 /unspam 的投票路径：校验 → 投票 → 进度回复 → 终局。

    指令入口的提示走 ``spam_vote.command.*``（消息回复 + 自删），与按钮 toast
    分开维护文案；达阈值时复用 ``finalize_vote_if_ready`` 保证与按钮路径同一
    互斥语义。
    """
    assert message.from_user  # 调用方（cmd_spam / cmd_unspam）已保证
    chat_id = message.chat.id

    session = await get_vote_session(chat_id, target_message_id)
    if session is None:
        reply = await message.answer(localizer.t("spam_vote.command.no_session.message"))
        await auto_delete_message(reply)
        return

    eligibility = await check_vote_eligibility(bot, chat_id, message.from_user.id, session)
    if eligibility != "ok":
        reply = await message.answer(localizer.t(_ELIGIBILITY_MESSAGE_KEYS[eligibility]))
        await auto_delete_message(reply)
        return

    outcome: VoteOutcome = await cast_vote(
        chat_id,
        target_message_id,
        message.from_user.id,
        direction,
        expected_vote_id=session.vote_id,
    )
    if outcome.status in {"no_session", "mismatch"}:
        # 竞态：读取会话后、投票前被终局消费或重建
        reply = await message.answer(localizer.t("spam_vote.command.no_session.message"))
        await auto_delete_message(reply)
        return
    if outcome.status == "already":
        reply = await message.answer(localizer.t("spam_vote.command.already_voted.message"))
        await auto_delete_message(reply)
        return

    finalized = await finalize_vote_if_ready(
        bot,
        chat_id,
        target_message_id,
        session,
        message.from_user.id,
        direction,
        outcome.up,
        outcome.down,
    )
    if finalized:
        return  # 终局文案已编辑进提示消息

    reply = await message.answer(
        localizer.t(
            "spam_vote.command.voted.message",
            up=outcome.up,
            down=outcome.down,
            threshold=session.threshold,
        )
    )
    await auto_delete_message(reply, delay=30)
    await edit_vote_progress(
        bot, chat_id, target_message_id, outcome.up, outcome.down, session.threshold
    )


@router.callback_query(F.data.startswith("spam_vote:"))
async def on_spam_vote_callback(callback: CallbackQuery, bot: Bot) -> None:
    """处理成员投票按钮（callback_data: ``spam_vote:{up|down}:{orig_msg_id}:{vote_id}``）。

    只 answer 一次：``cast_vote`` 是本地 Redis 调用（毫秒级）无需 processing 预答，
    结果 toast（已投进度 / 已投过 / 已结束）在投票后统一发出；随后的终局
    ``finalize_vote_if_ready`` 不再 answer（Telegram 仅允许一次）。
    """
    if (
        not callback.data
        or not callback.message
        or isinstance(callback.message, InaccessibleMessage)
    ):
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
        return

    message: Message = callback.message
    try:
        prefix, direction, orig_msg_id_raw, vote_id = callback.data.split(":", 3)
        orig_msg_id = int(orig_msg_id_raw)
    except ValueError:
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
        return

    if (
        prefix != "spam_vote"
        or direction not in {"up", "down"}
        or orig_msg_id <= 0
        or _VOTE_ID_PATTERN.fullmatch(vote_id) is None
    ):
        await _answer_toast(callback, "antispam.callback.invalid_data.toast")
        return

    chat_id = message.chat.id
    # 白名单校验后值域已定为 up / down
    vote_direction = cast("Literal['up', 'down']", direction)
    session = await get_vote_session(chat_id, orig_msg_id)
    if session is None:
        # 会话已过期/已终局：按钮失效，自愈删除残留提示（对齐 review tombstone）
        await _answer_toast(callback, "spam_vote.callback.expired.toast")
        with contextlib.suppress(Exception):
            await message.delete()
        return

    eligibility = await check_vote_eligibility(bot, chat_id, callback.from_user.id, session)
    if eligibility != "ok":
        await _answer_toast(callback, _ELIGIBILITY_TOAST_KEYS[eligibility])
        return

    outcome = await cast_vote(
        chat_id,
        orig_msg_id,
        callback.from_user.id,
        vote_direction,
        expected_vote_id=vote_id,
    )
    if outcome.status in {"no_session", "mismatch"}:
        await _answer_toast(callback, "spam_vote.callback.expired.toast")
        return
    if outcome.status == "already":
        await _answer_toast(callback, "spam_vote.callback.already_voted.toast")
        return

    await _answer_toast(
        callback,
        "spam_vote.callback.voted.toast",
        show_alert=False,
        up=outcome.up,
        down=outcome.down,
        threshold=session.threshold,
    )

    finalized = await finalize_vote_if_ready(
        bot,
        chat_id,
        orig_msg_id,
        session,
        callback.from_user.id,
        vote_direction,
        outcome.up,
        outcome.down,
        prompt_message=message,
    )
    if finalized:
        return

    await edit_vote_progress(bot, chat_id, orig_msg_id, outcome.up, outcome.down, session.threshold)


@router.callback_query(F.data.startswith("groupset_spamvote_toggle:"))
async def on_groupset_spamvote_toggle(callback: CallbackQuery, localizer: BoundLocalizer) -> None:
    """处理集体投票群级开关（callback_data: ``groupset_spamvote_toggle:{chat_id}:{on|off}``）。

    范式对齐 ``on_antichannel_toggle``：先 answer（DB 已持久化）再重绘子菜单，
    edit 失败不影响已生效设置。关闭语义为「冻结」：在途会话不可再投票、不会达
    阈值，1 小时后自然过期；管理员直达处理不受影响。
    """
    if (
        not callback.data
        or not callback.message
        or isinstance(callback.message, InaccessibleMessage)
    ):
        await callback.answer(
            localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
        )
        return

    message: Message = callback.message
    try:
        _, chat_id_raw, action = callback.data.split(":")
        chat_id = int(chat_id_raw)
    except ValueError:
        await callback.answer(
            localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
        )
        return

    if message.chat.id != chat_id or action not in {"on", "off"}:
        await callback.answer(
            localizer.t("admin.groupset.callback.invalid_operation.toast"), show_alert=True
        )
        return

    if callback.from_user.id not in settings.admin_ids:
        if not await PermissionCache.is_admin(callback.bot, chat_id, callback.from_user.id):  # type: ignore[arg-type]
            await callback.answer(
                localizer.t("admin.groupset.callback.permission_denied.toast"),
                show_alert=True,
            )
            return

    enabled = action == "on"
    updated = await GroupRepository.update_spam_vote_settings(chat_id, enabled)
    if not updated:
        await callback.answer(localizer.t("admin.groupset.callback.failed.toast"), show_alert=True)
        return

    state = "enabled" if enabled else "disabled"
    common_status = localizer.t(f"admin.common.status.{state}.label")
    status = localizer.t(f"admin.groupset.status.{state}.label", status=common_status)

    # 先确认 callback（DB 已持久化），再更新 UI；edit_text 失败不影响已生效设置
    await callback.answer(show_alert=False)
    try:
        await message.edit_text(
            localizer.t(
                "admin.groupset.menu.spamvote.message",
                status=status,
                threshold=settings.spam_vote_threshold,
            ),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as edit_exc:
        logger.warning(f"集体投票 toggle edit_text 失败(设置已生效): {edit_exc}")

    logger.info(f"群组 {chat_id} 集体投票功能切换为 {state}")
