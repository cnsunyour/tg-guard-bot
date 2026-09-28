"""自定义验证脚本管理命令（/customverify）与 groupset 开关。

交互流程：
- 群内 `/customverify upload` → 写等待状态键 → 引导管理员到私聊发脚本文件
- 私聊文档消息命中等待键 → 下载校验 → 三重审查（静态/AI/沙盒 dry-run）→
  入库并提示 enable
- `enable/disable/rollback/status/history` 子命令直接操作 revision 激活指针

安全边界：群内命令走 message 级管理员校验；私聊文档处理走「等待键归属 +
上传者管理员身份」双重校验——等待键只由群内命令写入，天然绑定发起群。
"""

from __future__ import annotations

import contextlib
import io

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InaccessibleMessage, Message
from loguru import logger

from src.core.cache import PermissionCache
from src.core.config import settings
from src.core.i18n import BoundLocalizer, get_resolver, get_translator
from src.core.redis import RedisKeys, get_redis
from src.core.utils import check_admin_permission, check_admin_permission_by_id
from src.repositories.audit_repo import AuditRepository
from src.repositories.custom_verification_repo import CustomVerificationRepository
from src.repositories.group_repo import GroupRepository
from src.services.custom_verification import get_custom_verification_service
from src.services.sandbox_client import get_sandbox_client

router = Router(name="custom_verify")

# 与 sandbox/protocol.MAX_SOURCE_BYTES 对齐（单处改动需同步）
_MAX_SOURCE_BYTES = 64 * 1024
_ALLOWED_EXTENSIONS = (".py", ".js")
_UPLOAD_WAIT_SECONDS = 300
_HISTORY_LIMIT = 10


def _sandbox_ready() -> tuple[bool, bool]:
    """返回 (全局开关开, 沙盒已配置)。"""
    return settings.custom_verification_enabled, get_sandbox_client().configured


async def _audit(chat_id: int, operator_id: int, action: str, details: dict) -> None:
    # 审计失败不影响主业务流（项目纪律：contextlib.suppress 包裹）
    with contextlib.suppress(Exception):
        await AuditRepository.log_action(
            group_id=chat_id, operator_id=operator_id, action=action, details=details
        )


def _review_status_label(review_result: object, localizer: BoundLocalizer) -> str:
    """revision 审查状态的人类可读摘要（history 列表用）。"""
    if not isinstance(review_result, dict):
        return localizer.t("customverify.review.pending.label")
    static_ok = (
        isinstance(review_result.get("static"), dict)
        and review_result["static"].get("passed") is True
    )
    ai = review_result.get("ai")
    ai_ok = isinstance(ai, dict) and ai.get("risk") == "safe"
    dry = review_result.get("dry_run")
    dry_ok = isinstance(dry, dict) and dry.get("passed") is True
    if static_ok and ai_ok and dry_ok:
        return localizer.t("customverify.review.passed.label")
    return localizer.t("customverify.review.failed.label")


@router.message(Command("customverify"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_customverify(
    message: Message, bot: Bot, localizer: BoundLocalizer, command: CommandObject
) -> None:
    """自定义验证脚本管理入口（群聊，管理员）。

    子命令：status（默认）｜upload｜enable <id>｜disable｜rollback <id>｜history
    """
    if not message.from_user or not message.chat:
        return
    chat_id = message.chat.id
    operator_id = message.from_user.id

    # message 版权限检查：可识别匿名管理员（sender_chat 场景 by_id 会误拒）
    if not await check_admin_permission(message, bot):
        await message.answer(localizer.t("customverify.permission_denied.message"))
        return

    enabled, sandbox_ready = _sandbox_ready()
    if not enabled:
        await message.answer(localizer.t("customverify.globally_disabled.message"))
        return

    args = (command.args or "").split()
    subcommand = args[0].lower() if args else "status"

    # 子命令统一入口（upload 之外的读写操作都要求沙盒已配置）
    if subcommand == "upload":
        if not sandbox_ready:
            await message.answer(localizer.t("customverify.sandbox_missing.message"))
            return
        await _handle_upload_request(message, localizer, chat_id, operator_id)
        return

    if subcommand in ("status", ""):
        await _show_status(message, localizer, chat_id)
        return
    if subcommand == "history":
        await _show_history(message, localizer, chat_id)
        return
    if subcommand in ("enable", "rollback"):
        if not sandbox_ready:
            await message.answer(localizer.t("customverify.sandbox_missing.message"))
            return
        await _handle_activate(
            message, localizer, chat_id, operator_id, args, rollback=(subcommand == "rollback")
        )
        return
    if subcommand == "disable":
        await _handle_disable(message, localizer, chat_id, operator_id)
        return

    await _show_status(message, localizer, chat_id)


async def _handle_upload_request(
    message: Message, localizer: BoundLocalizer, chat_id: int, operator_id: int
) -> None:
    """进入等待上传状态并引导管理员去私聊发文件。"""
    redis = get_redis()
    await redis.set(
        RedisKeys.custom_verify_upload(operator_id),
        str(chat_id),
        ex=_UPLOAD_WAIT_SECONDS,
    )
    await message.answer(localizer.t("customverify.upload.prompt.message"))


async def _show_status(message: Message, localizer: BoundLocalizer, chat_id: int) -> None:
    group = await GroupRepository.get_or_create(chat_id)
    if group is None:
        await message.answer(localizer.t("customverify.disable.nothing.message"))
        return

    lines: list[str] = []
    if group.custom_verify_enabled and group.active_revision_id is not None:
        status = localizer.t("admin.common.status.enabled.label")
        lines.append(localizer.t("customverify.status.enabled.header", status=status))
        revision = await CustomVerificationRepository.get_group_revision(
            chat_id, group.active_revision_id
        )
        if revision is not None:
            lines.append(
                localizer.t(
                    "customverify.status.detail.line",
                    revision_id=revision.id,
                    language=revision.language,
                    date=revision.created_at.strftime("%Y-%m-%d") if revision.created_at else "-",
                )
            )
    else:
        status = localizer.t("admin.common.status.disabled.label")
        lines.append(localizer.t("customverify.status.enabled.header", status=status))
        lines.append(localizer.t("customverify.status.none.line"))
    lines.append(localizer.t("customverify.status.usage.line"))
    await message.answer("\n".join(lines))


async def _show_history(message: Message, localizer: BoundLocalizer, chat_id: int) -> None:
    revisions = await CustomVerificationRepository.list_group_revisions(
        chat_id, limit=_HISTORY_LIMIT
    )
    if not revisions:
        await message.answer(localizer.t("customverify.history.empty.message"))
        return
    lines = [localizer.t("customverify.history.header")]
    for revision in revisions:
        lines.append(
            localizer.t(
                "customverify.history.line",
                id=revision.id,
                language=revision.language,
                status=_review_status_label(revision.review_result, localizer),
                date=revision.created_at.strftime("%Y-%m-%d") if revision.created_at else "-",
            )
        )
    await message.answer("\n".join(lines))


async def _handle_activate(
    message: Message,
    localizer: BoundLocalizer,
    chat_id: int,
    operator_id: int,
    args: list[str],
    *,
    rollback: bool,
) -> None:
    """enable/rollback 共用：校验数字参数 → service 激活 → 反馈 + 审计。"""
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(localizer.t("customverify.status.usage.line"))
        return
    revision_id = int(args[1])
    service = get_custom_verification_service()
    ok = await service.activate(chat_id, revision_id)
    action = "custom_verify_revision_rollback" if rollback else "custom_verify_revision_activate"
    await _audit(chat_id, operator_id, action, {"revision_id": revision_id, "ok": ok})
    if ok:
        key = (
            "customverify.rollback.success.message"
            if rollback
            else "customverify.enable.success.message"
        )
    else:
        key = "customverify.enable.failed.message"
    await message.answer(localizer.t(key, revision_id=revision_id))


async def _handle_disable(
    message: Message, localizer: BoundLocalizer, chat_id: int, operator_id: int
) -> None:
    service = get_custom_verification_service()
    group = await GroupRepository.get_or_create(chat_id)
    current = group.active_revision_id if group else None
    if group is None or current is None or not group.custom_verify_enabled:
        await message.answer(localizer.t("customverify.disable.nothing.message"))
        return
    ok = await service.disable(chat_id)
    await _audit(
        chat_id, operator_id, "custom_verify_revision_disable", {"revision_id": current, "ok": ok}
    )
    if ok:
        await message.answer(
            localizer.t("customverify.disable.success.message", revision_id=current)
        )
    else:
        await message.answer(localizer.t("customverify.disable.nothing.message"))


@router.message(F.chat.type == "private", F.document)
async def on_custom_verify_document(message: Message, bot: Bot) -> None:
    """私聊脚本文件上传：仅处理命中等待键的文档，其余私聊文档不拦截。"""
    if not message.from_user or not message.document or not message.document.file_name:
        return
    operator_id = message.from_user.id
    redis = get_redis()
    # GETDEL 原子取删：同一管理员并发发送多个文档时只有第一个能取到键
    chat_id_raw = await redis.getdel(RedisKeys.custom_verify_upload(operator_id))
    if not chat_id_raw:
        return  # 非上传流程的私聊文档：静默忽略（不 answer，避免误吞其他 bot 交互）
    chat_id = int(chat_id_raw)

    # 等待键归属 + 上传者管理员身份双重校验（键只由群内命令写入，这里再查一次权限）
    if not await check_admin_permission_by_id(bot, chat_id, operator_id):
        return

    locale = await get_resolver().for_private_from_group(user_id=operator_id, group_chat_id=chat_id)
    localizer = get_translator().for_locale(locale)
    document = message.document

    file_name = document.file_name or ""
    if not file_name.lower().endswith(_ALLOWED_EXTENSIONS):
        await message.answer(localizer.t("customverify.upload.bad_ext.message"))
        return
    if (document.file_size or 0) > _MAX_SOURCE_BYTES:
        await message.answer(localizer.t("customverify.upload.too_large.message"))
        return

    enabled, sandbox_ready = _sandbox_ready()
    if not enabled:
        await message.answer(localizer.t("customverify.upload.globally_disabled.message"))
        return
    if not sandbox_ready:
        await message.answer(localizer.t("customverify.sandbox_missing.message"))
        return

    await message.answer(localizer.t("customverify.upload.received.message", filename=file_name))

    # 下载到内存（脚本 ≤64KB，无落盘必要）
    buffer = io.BytesIO()
    try:
        await bot.download(document, destination=buffer)
    except Exception as exc:
        logger.warning(f"脚本文件下载失败 [群组:{chat_id}] [管理员:{operator_id}]: {exc}")
        await message.answer(localizer.t("customverify.upload.download_failed.message"))
        return
    # file_size 是客户端声明可伪造，以下载后的实际字节数复验
    if len(buffer.getvalue()) > _MAX_SOURCE_BYTES:
        await message.answer(localizer.t("customverify.upload.too_large.message"))
        return
    try:
        source = buffer.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        await message.answer(localizer.t("customverify.upload.bad_ext.message"))
        return

    language = "python" if file_name.lower().endswith(".py") else "javascript"
    service = get_custom_verification_service()
    try:
        outcome = await service.review_and_save(chat_id, language, source, uploaded_by=operator_id)
    except Exception as exc:
        logger.error(f"脚本审查未预期异常 [群组:{chat_id}] [管理员:{operator_id}]: {exc}")
        await _audit(
            chat_id,
            operator_id,
            "custom_verify_revision_upload",
            {"language": language, "ok": False, "stage": "unexpected_error"},
        )
        await message.answer(localizer.t("customverify.upload.failed.header", stage="error"))
        return
    await _audit(
        chat_id,
        operator_id,
        "custom_verify_revision_upload",
        {
            "revision_id": outcome.revision.id if outcome.revision else None,
            "language": language,
            "ok": outcome.ok,
            "stage": outcome.stage,
        },
    )

    if outcome.ok and outcome.revision is not None:
        await message.answer(
            localizer.t("customverify.upload.success.message", revision_id=outcome.revision.id)
        )
        logger.info(
            f"自定义脚本上传成功 [群组:{chat_id}] [管理员:{operator_id}] "
            f"[revision:{outcome.revision.id}]"
        )
    else:
        lines = [localizer.t("customverify.upload.failed.header", stage=outcome.stage)]
        lines.extend(f"• {violation}" for violation in outcome.violations[:10])
        await message.answer("\n".join(lines))
        logger.info(
            f"自定义脚本上传被拒 [群组:{chat_id}] [管理员:{operator_id}] [stage:{outcome.stage}]"
        )


@router.callback_query(F.data.startswith("groupset_customverify_toggle:"))
async def on_groupset_customverify_toggle(
    callback: CallbackQuery, localizer: BoundLocalizer
) -> None:
    """groupset 子面板的脚本验证开关（callback: ``groupset_customverify_toggle:{chat_id}:{on|off}``）。

    范式对齐 on_groupset_spamvote_toggle：先 answer（DB 已持久化）再重绘。
    关闭 = 只切 enabled 开关，revision 指针保留（重新启用恢复同一版本）。
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

    enabled, _ = _sandbox_ready()
    if not enabled:
        # 全局门禁与命令路径一致：未启用时不允许改群级状态
        await callback.answer(
            localizer.t("customverify.globally_disabled.message"), show_alert=True
        )
        return

    group = await GroupRepository.get_or_create(chat_id)
    if group is None or group.active_revision_id is None:
        # 无已激活版本时开关无意义：引导先上传
        await callback.answer(
            localizer.t("admin.groupset.customverify.summary.none"), show_alert=True
        )
        return

    enabled = action == "on"
    from src.repositories.custom_verification_repo import CustomVerificationRepository

    updated = await CustomVerificationRepository.set_custom_verify_enabled(chat_id, enabled)
    if not updated:
        await callback.answer(localizer.t("admin.groupset.callback.failed.toast"), show_alert=True)
        return
    await _audit(
        chat_id,
        callback.from_user.id,
        "custom_verify_toggle",
        {"enabled": enabled, "revision_id": group.active_revision_id},
    )

    state = "enabled" if enabled else "disabled"
    common_status = localizer.t(f"admin.common.status.{state}.label")
    status = localizer.t(f"admin.groupset.status.{state}.label", status=common_status)
    summary = localizer.t(
        "admin.groupset.customverify.summary.active", revision_id=group.active_revision_id
    )

    # 先确认 callback（DB 已持久化），再更新 UI；edit_text 失败不影响已生效设置
    await callback.answer(show_alert=False)
    with contextlib.suppress(Exception):
        await message.edit_text(
            localizer.t("admin.groupset.menu.customverify.message", status=status, summary=summary),
            parse_mode="HTML",
            reply_markup=None,
        )
    logger.info(f"群组 {chat_id} 自定义验证脚本切换为 {state}")
