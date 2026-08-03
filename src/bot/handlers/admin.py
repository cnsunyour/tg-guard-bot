"""管理员配置命令处理器"""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from src.core.cache import PermissionCache
from src.core.config import settings
from src.core.health import get_health_checker
from src.core.i18n import BoundLocalizer, get_resolver, get_translator
from src.core.utils import auto_delete_message, check_admin_permission, escape_html
from src.repositories.group_repo import GroupRepository

router = Router(name="admin")

# 验证方式顺序(按钮排列),派生白名单避免重复维护
_VERIFICATION_TYPES = (
    "math",
    "slider",
    "qa",
    "emoji",
    "captcha",
    "honeypot",
    "puzzle",
    "turnstile",
    "friendly",
    "hcaptcha",
    "mtcaptcha",
    "altcha",
    "random",
)
_VALID_VERIFICATION_TYPES = frozenset(_VERIFICATION_TYPES)

# /groupset 子菜单类型白名单(校验 callback_data)
_GROUPSET_MENU_TYPES = frozenset(
    {"verify", "timeout", "antispam", "antichannel", "activity", "activityskip"}
)


def _build_setverify_keyboard(
    localizer: BoundLocalizer,
    chat_id: int,
) -> InlineKeyboardMarkup:
    """构建验证方式选择键盘(13 按钮,文案走 setverify.verification_type.<type>.button)。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t(f"admin.setverify.verification_type.{verify_type}.button"),
                    callback_data=f"setverify:{chat_id}:{verify_type}",
                )
            ]
            for verify_type in _VERIFICATION_TYPES
        ]
    )


def _render_activity_panel(
    localizer: BoundLocalizer,
    chat_id: int,
    enabled: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """渲染活跃度系统设置面板(状态文本 + 启用/禁用键盘)。

    状态文案双层:common.status.<state>.label 注入 activity.status.<state>.label,
    使 emoji 排列归 catalog管理、公共词项复用。
    """
    state = "enabled" if enabled else "disabled"
    common_status = localizer.t(f"admin.common.status.{state}.label")
    status = localizer.t(f"admin.activity.status.{state}.label", status=common_status)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.activity.enable.button"),
                    callback_data=f"activity:{chat_id}:enable",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.activity.disable.button"),
                    callback_data=f"activity:{chat_id}:disable",
                )
            ],
        ]
    )
    return localizer.t("admin.activity.panel.message", status=status), keyboard


def _groupset_status_label(localizer: BoundLocalizer, enabled: bool) -> str:
    """groupset 主菜单状态文案:common.status 注入 groupset.status(emoji 在 catalog)。"""
    state = "enabled" if enabled else "disabled"
    common_status = localizer.t(f"admin.common.status.{state}.label")
    return localizer.t(f"admin.groupset.status.{state}.label", status=common_status)


def _with_groupset_back_button(
    localizer: BoundLocalizer,
    chat_id: int,
    keyboard: InlineKeyboardMarkup | None = None,
) -> InlineKeyboardMarkup:
    """在给定键盘末尾追加"返回主菜单"按钮(无键盘则仅返回按钮行)。

    不修改 3c7 的 _build_setverify_keyboard / _render_activity_panel 公共 API,
    groupset 子菜单在此追加返回按钮。
    """
    rows = [list(row) for row in keyboard.inline_keyboard] if keyboard else []
    rows.append(
        [
            InlineKeyboardButton(
                text=localizer.t("admin.groupset.back.button"),
                callback_data=f"groupset_back:{chat_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_groupset_main_menu(
    localizer: BoundLocalizer,
    chat_id: int,
    verification_type: str | None,
    *,
    antispam_enabled: bool,
    antichannel_enabled: bool,
    activity_enabled: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """渲染 groupset 主菜单(状态报告 + 6 配置入口按钮)。

    verification_type 非白名单值(含 None)统一回退到 unknown 短标签。
    """
    verification_key = (
        verification_type if verification_type in _VALID_VERIFICATION_TYPES else "unknown"
    )
    verification_label = localizer.t(
        f"admin.groupset.verification_type.{verification_key}.short.label"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.groupset.menu.verify.button"),
                    callback_data=f"groupset_menu:{chat_id}:verify",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.groupset.menu.timeout.button"),
                    callback_data=f"groupset_menu:{chat_id}:timeout",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.groupset.menu.antispam.button"),
                    callback_data=f"groupset_menu:{chat_id}:antispam",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.groupset.menu.antichannel.button"),
                    callback_data=f"groupset_menu:{chat_id}:antichannel",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.groupset.menu.activity.button"),
                    callback_data=f"groupset_menu:{chat_id}:activity",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.t("admin.groupset.menu.activityskip.button"),
                    callback_data=f"groupset_menu:{chat_id}:activityskip",
                )
            ],
        ]
    )

    return (
        localizer.t(
            "admin.groupset.main.message",
            verification_type=verification_label,
            antispam_status=_groupset_status_label(localizer, antispam_enabled),
            antichannel_status=_groupset_status_label(localizer, antichannel_enabled),
            activity_status=_groupset_status_label(localizer, activity_enabled),
        ),
        keyboard,
    )


async def show_command_overview(message: Message) -> None:
    """显示完整命令概览（由 /help 无参时调用，非 /start 路由）。

    /start 路由由 start.py 唯一注册；本函数去装饰器避免重复注册不可达。
    按消息所在 chat locale 渲染（私聊 for_user、群 for_group）。
    """
    if not message.from_user:
        return
    resolver = get_resolver()
    if message.chat.type == "private":
        locale = await resolver.for_user(message.from_user.id)
    else:
        locale = await resolver.for_group(message.chat.id)
    localizer = get_translator().for_locale(locale)
    await message.answer(
        localizer.t("admin.help.overview.message"),
        parse_mode="HTML",
    )


# 命令详细帮助文档
# 支持详细帮助的命令白名单（文档走 catalog admin.help.detail.<command>.message）
_HELP_COMMANDS = frozenset(
    {
        "activity",
        "activityskip",
        "antichannel",
        "antispam",
        "approve",
        "ban",
        "cleanup",
        "clearwarnings",
        "curfew",
        "delafter",
        "delbefore",
        "delrange",
        "groupset",
        "help",
        "kick",
        "mute",
        "nospam",
        "notspam",
        "reject",
        "report",
        "reports",
        "settimeout",
        "setverify",
        "spam",
        "unban",
        "unmute",
        "unspam",
        "verifyconfig",
        "warn",
        "warnings",
        "whitelist",
    }
)


@router.message(Command("help"))
async def cmd_help(message: Message, localizer: BoundLocalizer) -> None:
    """帮助命令 - 支持查看具体命令的详细用法"""
    # 类型检查
    if not message.text:
        return

    # 解析参数
    args = message.text.split(maxsplit=1)

    # 如果没有参数，显示通用帮助（命令概览）
    if len(args) == 1:
        await show_command_overview(message)
        return

    # 获取命令名称（去掉可能的 / 前缀）
    command = args[1].lstrip("/").lower()

    # 查找命令帮助（文档走 catalog；_HELP_COMMANDS 作白名单）
    if command in _HELP_COMMANDS:
        await message.answer(localizer.t(f"admin.help.detail.{command}.message"), parse_mode="HTML")
    else:
        await message.answer(
            localizer.t("admin.help.not_found.message", command=escape_html(command)),
            parse_mode="HTML",
        )


@router.message(Command("groupset"))
async def cmd_groupset(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """群组设置主菜单（统一配置入口）"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("admin.groupset.error.group_only.message"))
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer(localizer.t("admin.groupset.error.admin_only.message"))
        return

    # 获取当前配置状态
    try:
        group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)
    except Exception as e:
        logger.error(f"获取群组配置失败: {e}")
        reply = await message.answer(localizer.t("admin.groupset.error.load_failed.message"))
        await auto_delete_message(reply)
        return

    text, keyboard = _render_groupset_main_menu(
        localizer,
        message.chat.id,
        group.verification_type,
        antispam_enabled=group.antispam_enabled,
        antichannel_enabled=group.anti_channel_enabled,
        activity_enabled=group.activity_enabled,
    )
    reply = await message.answer(text, reply_markup=keyboard)
    await auto_delete_message(reply)


@router.callback_query(F.data.startswith("groupset_menu:"))
async def on_groupset_menu(callback: CallbackQuery, bot: Bot, localizer: BoundLocalizer) -> None:
    """处理群组设置菜单回调"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
            )
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer(
                localizer.t("admin.groupset.callback.message_unavailable.toast"),
                show_alert=True,
            )
            return

        message: Message = callback.message

        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer(
                localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
            )
            return
        _, chat_id_str, menu_type = parts
        chat_id = int(chat_id_str)

        # 校验 callback 所属群与 menu_type 白名单
        if message.chat.id != chat_id or menu_type not in _GROUPSET_MENU_TYPES:
            await callback.answer(
                localizer.t("admin.groupset.callback.invalid_operation.toast"),
                show_alert=True,
            )
            return

        # 权限验证
        if callback.from_user.id not in settings.admin_ids:
            if not await PermissionCache.is_admin(bot, chat_id, callback.from_user.id):
                await callback.answer(
                    localizer.t("admin.groupset.callback.permission_denied.toast"),
                    show_alert=True,
                )
                return

        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id)

        # 根据菜单类型显示不同的配置界面
        if menu_type == "verify":
            keyboard = _with_groupset_back_button(
                localizer, chat_id, _build_setverify_keyboard(localizer, chat_id)
            )
            await message.edit_text(
                localizer.t("admin.setverify.prompt.message"), reply_markup=keyboard
            )

        elif menu_type == "timeout":
            timeout = group.verification_timeout or settings.verification_timeout
            await message.edit_text(
                localizer.t("admin.groupset.menu.timeout.message", timeout=timeout),
                reply_markup=_with_groupset_back_button(localizer, chat_id),
            )

        elif menu_type == "antispam":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=localizer.t("admin.groupset.menu.antispam.enable.button"),
                            callback_data=f"antispam_toggle:{chat_id}:on",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=localizer.t("admin.groupset.menu.antispam.disable.button"),
                            callback_data=f"antispam_toggle:{chat_id}:off",
                        )
                    ],
                ]
            )
            await message.edit_text(
                localizer.t(
                    "admin.groupset.menu.antispam.message",
                    status=_groupset_status_label(localizer, group.antispam_enabled),
                ),
                reply_markup=_with_groupset_back_button(localizer, chat_id, keyboard),
            )

        elif menu_type == "antichannel":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=localizer.t("admin.groupset.menu.antichannel.enable.button"),
                            callback_data=f"antichannel_toggle:{chat_id}:on",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=localizer.t("admin.groupset.menu.antichannel.disable.button"),
                            callback_data=f"antichannel_toggle:{chat_id}:off",
                        )
                    ],
                ]
            )
            await message.edit_text(
                localizer.t(
                    "admin.groupset.menu.antichannel.message",
                    status=_groupset_status_label(localizer, group.anti_channel_enabled),
                ),
                reply_markup=_with_groupset_back_button(localizer, chat_id, keyboard),
            )

        elif menu_type == "activity":
            text, keyboard = _render_activity_panel(localizer, chat_id, group.activity_enabled)
            await message.edit_text(
                text, reply_markup=_with_groupset_back_button(localizer, chat_id, keyboard)
            )

        elif menu_type == "activityskip":
            global_threshold = settings.activity_skip_spam_check_threshold
            group_threshold = group.activity_skip_threshold
            if global_threshold > 0:
                effective_threshold = global_threshold
                threshold_source = localizer.t("admin.activityskip.source.global.label")
            elif global_threshold == 0:
                effective_threshold = group_threshold
                threshold_source = localizer.t("admin.activityskip.source.group.label")
            else:
                effective_threshold = 0
                threshold_source = localizer.t("admin.activityskip.source.disabled.label")
            await message.edit_text(
                localizer.t(
                    "admin.groupset.menu.activityskip.message",
                    group_threshold=group_threshold,
                    global_threshold=global_threshold,
                    effective_threshold=effective_threshold,
                    threshold_source=threshold_source,
                ),
                reply_markup=_with_groupset_back_button(localizer, chat_id),
            )

        await callback.answer()

    except ValueError:
        # callback.data 分割不足或 int() 失败
        await callback.answer(
            localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
        )
    except Exception as e:
        logger.error(f"处理群组设置菜单回调失败: {e}")
        await callback.answer(localizer.t("admin.groupset.callback.failed.toast"), show_alert=True)


@router.callback_query(F.data.startswith("groupset_back:"))
async def on_groupset_back(callback: CallbackQuery, bot: Bot, localizer: BoundLocalizer) -> None:
    """返回群组设置主菜单"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
            )
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer(
                localizer.t("admin.groupset.callback.message_unavailable.toast"),
                show_alert=True,
            )
            return

        message: Message = callback.message

        parts = callback.data.split(":")
        if len(parts) != 2:
            await callback.answer(
                localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
            )
            return
        _, chat_id_str = parts
        chat_id = int(chat_id_str)

        # 校验 callback 所属群
        if message.chat.id != chat_id:
            await callback.answer(
                localizer.t("admin.groupset.callback.invalid_operation.toast"),
                show_alert=True,
            )
            return

        # 权限验证
        if callback.from_user.id not in settings.admin_ids:
            if not await PermissionCache.is_admin(bot, chat_id, callback.from_user.id):
                await callback.answer(
                    localizer.t("admin.groupset.callback.permission_denied.toast"),
                    show_alert=True,
                )
                return

        # 获取当前配置状态
        group = await GroupRepository.get_or_create(chat_id)

        text, keyboard = _render_groupset_main_menu(
            localizer,
            chat_id,
            group.verification_type,
            antispam_enabled=group.antispam_enabled,
            antichannel_enabled=group.anti_channel_enabled,
            activity_enabled=group.activity_enabled,
        )
        await message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    except ValueError:
        await callback.answer(
            localizer.t("admin.groupset.callback.invalid_data.toast"), show_alert=True
        )
    except Exception as e:
        logger.error(f"返回群组设置主菜单失败: {e}")
        await callback.answer(localizer.t("admin.groupset.callback.failed.toast"), show_alert=True)


@router.message(Command("setverify"))
async def cmd_set_verify(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """设置验证方式"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("admin.setverify.error.group_only.message"))
        return

    # 检查权限（使用统一的权限检查函数）
    if not await check_admin_permission(message, bot):
        await message.answer(localizer.t("admin.setverify.error.admin_only.message"))
        return

    # 显示验证方式选择
    keyboard = _build_setverify_keyboard(localizer, message.chat.id)

    reply = await message.answer(
        localizer.t("admin.setverify.prompt.message"), reply_markup=keyboard
    )
    await auto_delete_message(reply)


@router.callback_query(F.data.startswith("setverify:"))
async def on_setverify_callback(callback: CallbackQuery, localizer: BoundLocalizer) -> None:
    """处理验证方式设置回调"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                localizer.t("admin.setverify.callback.invalid_data.toast"), show_alert=True
            )
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer(
                localizer.t("admin.setverify.callback.message_unavailable.toast"),
                show_alert=True,
            )
            return

        message: Message = callback.message

        _, chat_id_str, verify_type = callback.data.split(":")
        chat_id = int(chat_id_str)

        # 防御:回调消息所属群与 callback_data 中的 chat_id 必须一致
        if message.chat.id != chat_id:
            await callback.answer(
                localizer.t("admin.setverify.callback.invalid_operation.toast"),
                show_alert=True,
            )
            return

        # ✅ 权限验证
        if callback.from_user.id not in settings.admin_ids:
            try:
                member = await callback.bot.get_chat_member(chat_id, callback.from_user.id)  # type: ignore[union-attr]
                if member.status not in ["creator", "administrator"]:
                    await callback.answer(
                        localizer.t("admin.setverify.callback.permission_denied.toast"),
                        show_alert=True,
                    )
                    logger.warning(
                        f"用户 {callback.from_user.id} 尝试修改群组 {chat_id} 设置但无权限"
                    )
                    return
            except Exception as e:
                logger.error(f"权限检查失败: {e}")
                await callback.answer(
                    localizer.t("admin.setverify.callback.permission_check_failed.toast"),
                    show_alert=True,
                )
                return

        # ✅ 参数白名单验证
        if verify_type not in _VALID_VERIFICATION_TYPES:
            await callback.answer(
                localizer.t("admin.setverify.callback.invalid_type.toast"), show_alert=True
            )
            logger.warning(f"无效的验证类型: {verify_type}")
            return

        # 更新验证方式
        await GroupRepository.update_verification_type(chat_id, verify_type)

        # 成功提示:回显文案复用 common.verification_type.<type>.label(HTML 上下文)
        verify_type_label = localizer.t(f"admin.common.verification_type.{verify_type}.label")
        await message.edit_text(
            localizer.t("admin.setverify.result.saved.message", verify_type=verify_type_label)
        )
        await callback.answer(localizer.t("admin.setverify.callback.saved.toast"))

        logger.info(f"群组 {chat_id} 的验证方式已更新为 {verify_type}")

    except ValueError:
        # callback.data.split(":") 元素不足或 int() 失败
        await callback.answer(
            localizer.t("admin.setverify.callback.invalid_data.toast"), show_alert=True
        )
    except Exception as e:
        logger.error(f"设置验证方式失败: {e}")
        await callback.answer(
            localizer.t("admin.setverify.callback.save_failed.toast"), show_alert=True
        )


@router.message(Command("verifyconfig"))
async def cmd_verify_config(message: Message, localizer: BoundLocalizer) -> None:
    """查看验证配置"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("admin.verifyconfig.error.group_only.message"))
        return

    try:
        # 获取群组配置
        group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

        # 验证类型 label（未知类型或 strict 模式抛 KeyError 均回退到 escape 后的 code）
        verify_type_key = f"admin.common.verification_type.{group.verification_type}.label"
        try:
            verify_type_label = localizer.t(verify_type_key)
            if verify_type_label == verify_type_key:
                verify_type_label = escape_html(group.verification_type)
        except KeyError:
            # strict Translator 缺失 key 抛 KeyError（DEBUG 模式默认启用）
            verify_type_label = escape_html(group.verification_type)

        antispam_status = localizer.t(
            "admin.common.status.enabled.label"
            if group.antispam_enabled
            else "admin.common.status.disabled.label"
        )
        activity_status = localizer.t(
            "admin.common.status.enabled.label"
            if group.activity_enabled
            else "admin.common.status.disabled.label"
        )

        config_text = localizer.t(
            "admin.verifyconfig.report.message",
            verify_type=verify_type_label,
            timeout=group.verification_timeout,
            antispam_status=antispam_status,
            antispam_level=group.antispam_level,
            activity_status=activity_status,
        )

        reply = await message.answer(config_text)
        await auto_delete_message(reply)

        # 删除管理员的命令消息
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")

    except Exception as e:
        logger.error(f"查看验证配置失败: {e}")
        reply = await message.answer(localizer.t("admin.verifyconfig.error.load_failed.message"))
        await auto_delete_message(reply)

        # 删除管理员的命令消息
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")


@router.message(Command("settimeout"))
async def cmd_set_timeout(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """设置验证超时时间"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("admin.settimeout.error.group_only.message"))
        return

    # 检查管理员权限
    if not await check_admin_permission(message, bot):
        reply = await message.answer(localizer.t("admin.settimeout.error.admin_only.message"))
        await auto_delete_message(reply)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")
        return

    try:
        # 解析超时时间参数
        if not message.text:
            return

        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            reply = await message.answer(
                localizer.t("admin.settimeout.validation.missing_arg.message")
            )
            await auto_delete_message(reply)
            try:
                await message.delete()
            except Exception as e:
                logger.debug(f"删除命令消息失败: {e}")
            return

        # 验证参数
        try:
            timeout = int(args[1])
        except ValueError:
            reply = await message.answer(
                localizer.t("admin.settimeout.validation.not_integer.message")
            )
            await auto_delete_message(reply)
            try:
                await message.delete()
            except Exception as e:
                logger.debug(f"删除命令消息失败: {e}")
            return

        # 验证范围
        if not (30 <= timeout <= 300):
            reply = await message.answer(
                localizer.t("admin.settimeout.validation.out_of_range.message")
            )
            await auto_delete_message(reply)
            try:
                await message.delete()
            except Exception as e:
                logger.debug(f"删除命令消息失败: {e}")
            return

        # 更新群组配置
        await GroupRepository.get_or_create(message.chat.id, message.chat.title)
        await GroupRepository.update_verification_timeout(message.chat.id, timeout)

        reply = await message.answer(
            localizer.t("admin.settimeout.result.saved.message", timeout=timeout)
        )
        await auto_delete_message(reply)

        # 删除管理员的命令消息
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")

        logger.info(f"群组 {message.chat.id} 验证超时时间已设置为 {timeout} 秒")

    except Exception as e:
        logger.error(f"设置验证超时时间失败: {e}")
        reply = await message.answer(localizer.t("admin.settimeout.error.failed.message"))
        await auto_delete_message(reply)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")


@router.message(Command("health"))
async def cmd_health(message: Message, localizer: BoundLocalizer) -> None:
    """健康检查命令（仅超级管理员）"""
    # 检查是否是超级管理员
    if not message.from_user:
        return

    if message.from_user.id not in settings.admin_ids:
        await message.answer(localizer.t("admin.health.permission_denied.message"))
        return

    try:
        health_checker = get_health_checker()
        report = await health_checker.full_check()

        status_emoji = "✅" if report["healthy"] else "❌"
        uptime = report["uptime"]
        uptime_text = localizer.t(
            "admin.common.uptime.message",
            days=uptime["days"],
            hours=uptime["hours"],
            minutes=uptime["minutes"],
            seconds=uptime["seconds_component"],
        )

        db = report["database"]
        db_emoji = "✅" if db["healthy"] else "❌"
        db_error_line = (
            localizer.t(
                "admin.health.error_line.message", error=escape_html(str(db["error"])[:500])
            )
            if db["error"]
            else ""
        )

        redis = report["redis"]
        redis_emoji = "✅" if redis["healthy"] else "❌"
        redis_error_line = (
            localizer.t(
                "admin.health.error_line.message", error=escape_html(str(redis["error"])[:500])
            )
            if redis["error"]
            else ""
        )

        system_block = ""
        sys_metrics = report.get("system")
        if sys_metrics:
            system_block = localizer.t(
                "admin.health.report.system.message",
                cpu_percent=f"{sys_metrics['cpu']['percent']:.1f}",
                cpu_count=sys_metrics["cpu"]["count"],
                mem_used=f"{sys_metrics['memory']['used_mb']:.0f}",
                mem_total=f"{sys_metrics['memory']['total_mb']:.0f}",
                mem_percent=f"{sys_metrics['memory']['percent']:.1f}",
                disk_used=f"{sys_metrics['disk']['used_gb']:.1f}",
                disk_total=f"{sys_metrics['disk']['total_gb']:.1f}",
                disk_percent=f"{sys_metrics['disk']['percent']:.1f}",
            )

        text = localizer.t(
            "admin.health.report.message",
            status_emoji=status_emoji,
            uptime=uptime_text,
            check_count=report["check_count"],
            db_emoji=db_emoji,
            db_latency=f"{db['latency_ms']:.2f}",
            db_error_line=db_error_line,
            redis_emoji=redis_emoji,
            redis_latency=f"{redis['latency_ms']:.2f}",
            redis_error_line=redis_error_line,
            system_block=system_block,
        )

        await message.answer(text)

    except Exception:
        logger.exception("健康检查失败")
        # ✅ M2: 不向用户显示详细异常信息，防止信息泄露
        await message.answer(localizer.t("admin.health.failed.message"))


@router.message(Command("stats"))
async def cmd_stats(message: Message, localizer: BoundLocalizer) -> None:
    """统计信息命令（仅超级管理员）"""
    # 检查是否是超级管理员
    if not message.from_user:
        return

    if message.from_user.id not in settings.admin_ids:
        await message.answer(localizer.t("admin.stats.permission_denied.message"))
        return

    try:
        from src.services.spam_detector import get_detector

        # 获取反垃圾统计
        detector = get_detector()
        spam_stats = await detector.get_statistics()

        classifier_status = localizer.t(
            "admin.stats.classifier.trained.label"
            if spam_stats.get("classifier_trained")
            else "admin.stats.classifier.untrained.label"
        )
        embedder_status = localizer.t(
            "admin.stats.embedder.initialized.label"
            if spam_stats.get("embedder_initialized")
            else "admin.stats.embedder.uninitialized.label"
        )

        health_checker = get_health_checker()
        uptime = health_checker.get_uptime()
        uptime_text = localizer.t(
            "admin.common.uptime.message",
            days=uptime["days"],
            hours=uptime["hours"],
            minutes=uptime["minutes"],
            seconds=uptime["seconds_component"],
        )

        text = localizer.t(
            "admin.stats.report.message",
            total=spam_stats.get("total_samples", 0),
            spam=spam_stats.get("spam_samples", 0),
            normal=spam_stats.get("normal_samples", 0),
            classifier_status=classifier_status,
            embedder_status=embedder_status,
            uptime=uptime_text,
            started_at=uptime["started_at"],
        )

        await message.answer(text)

    except Exception:
        logger.exception("获取统计信息失败")
        # ✅ M2: 不向用户显示详细异常信息，防止信息泄露
        await message.answer(localizer.t("admin.stats.failed.message"))


async def _list_whitelist(message: Message, localizer: BoundLocalizer) -> None:
    """列出所有白名单群组"""
    try:
        # 获取所有白名单群组
        groups = await GroupRepository.get_whitelisted_groups()

        if not groups:
            await message.answer(localizer.t("admin.whitelist.list.empty.message"))
            return

        header = localizer.t("admin.whitelist.list.header.message", count=len(groups))
        parts = [header]
        for i, group in enumerate(groups, 1):
            title = (
                escape_html(group.title)
                if group.title
                else localizer.t("admin.common.unknown_group.label")
            )
            parts.append(
                localizer.t(
                    "admin.whitelist.list.row.message",
                    index=i,
                    title=title,
                    chat_id=group.id,
                )
            )
            if i < len(groups):
                parts.append("\n")

        await message.answer("".join(parts))

    except Exception:
        logger.exception("获取白名单列表失败")
        await message.answer(localizer.t("admin.whitelist.list.failed.message"))


async def _add_whitelist(message: Message, args: list[str], localizer: BoundLocalizer) -> None:
    """添加群组到白名单"""
    try:
        # 检查参数
        if len(args) < 3:
            await message.answer(localizer.t("admin.whitelist.add.error.missing_arg.message"))
            return

        chat_id = int(args[2])
        title = args[3] if len(args) > 3 else None

        # 获取或创建群组记录
        group = await GroupRepository.get_or_create(chat_id, title)

        # 群组显示标识：title 已 escape，无 title 用 chat_id（数字无需转义）
        group_display = escape_html(group.title) if group.title else chat_id

        if group.is_whitelisted:
            await message.answer(
                localizer.t("admin.whitelist.add.already_in.message", group=group_display)
            )
            return

        # 添加到白名单
        await GroupRepository.update_whitelist(chat_id, True)

        if not message.from_user:
            return

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 添加到白名单")
        await message.answer(localizer.t("admin.whitelist.add.saved.message", group=group_display))

    except ValueError:
        await message.answer(localizer.t("admin.whitelist.add.error.invalid_id.message"))
    except Exception:
        logger.exception("添加白名单失败")
        await message.answer(localizer.t("admin.whitelist.add.error.failed.message"))


async def _remove_whitelist(message: Message, args: list[str], localizer: BoundLocalizer) -> None:
    """从白名单移除群组"""
    try:
        # 检查参数
        if len(args) != 3:
            await message.answer(localizer.t("admin.whitelist.remove.error.missing_arg.message"))
            return

        chat_id = int(args[2])

        # 检查群组是否存在
        group = await GroupRepository.get_by_id(chat_id)
        if not group:
            await message.answer(
                localizer.t("admin.whitelist.remove.error.not_found.message", chat_id=chat_id)
            )
            return

        group_display = escape_html(group.title) if group.title else chat_id

        if not group.is_whitelisted:
            await message.answer(
                localizer.t("admin.whitelist.remove.not_in.message", group=group_display)
            )
            return

        # 从白名单移除
        await GroupRepository.update_whitelist(chat_id, False)

        if not message.from_user:
            return

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 从白名单移除")
        await message.answer(
            localizer.t("admin.whitelist.remove.saved.message", group=group_display)
        )

    except ValueError:
        await message.answer(localizer.t("admin.whitelist.remove.error.invalid_id.message"))
    except Exception:
        logger.exception("移除白名单失败")
        await message.answer(localizer.t("admin.whitelist.remove.error.failed.message"))


@router.message(Command("whitelist"))
async def cmd_whitelist(message: Message, localizer: BoundLocalizer) -> None:
    """白名单管理（仅超级管理员）

    用法：
    - /whitelist - 列出所有白名单群组
    - /whitelist add <chat_id> [群组名称] - 添加群组到白名单
    - /whitelist remove <chat_id> - 从白名单移除群组
    """
    if not message.from_user:
        return

    # 检查是否是超级管理员
    if message.from_user.id not in settings.admin_ids:
        await message.answer(localizer.t("admin.whitelist.error.permission_denied.message"))
        return

    if not message.text:
        return

    args = message.text.split(maxsplit=3)

    # 无参数 - 列出白名单
    if len(args) == 1:
        await _list_whitelist(message, localizer)
        return

    subcommand = args[1].lower()

    # add 子命令
    if subcommand == "add":
        await _add_whitelist(message, args, localizer)
        return

    # remove 子命令
    if subcommand == "remove":
        await _remove_whitelist(message, args, localizer)
        return

    # 未知子命令
    await message.answer(localizer.t("admin.whitelist.error.unknown_subcommand.message"))


@router.message(Command("activity"))
async def cmd_activity(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    """控制群组活跃度系统开关"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("admin.activity.error.group_only.message"))
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer(localizer.t("admin.activity.error.admin_only.message"))
        return

    # 显示活跃度控制面板
    try:
        group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

        text, keyboard = _render_activity_panel(localizer, message.chat.id, group.activity_enabled)
        reply = await message.answer(text, reply_markup=keyboard)
        await auto_delete_message(reply)

        # 删除管理员的命令消息
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")

    except Exception as e:
        logger.error(f"获取群组配置失败: {e}")
        await message.answer(localizer.t("admin.activity.error.load_failed.message"))


@router.callback_query(F.data.startswith("activity:"))
async def on_activity_callback(
    callback: CallbackQuery, bot: Bot, localizer: BoundLocalizer
) -> None:
    """处理活跃度设置回调"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer(
                localizer.t("admin.activity.callback.invalid_data.toast"), show_alert=True
            )
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer(
                localizer.t("admin.activity.callback.message_unavailable.toast"),
                show_alert=True,
            )
            return

        message: Message = callback.message

        # 解析回调数据
        _, chat_id_str, action = callback.data.split(":")
        chat_id = int(chat_id_str)

        # 检查权限（回调所在群与 callback_data 中的 chat_id 必须一致）
        if message.chat.id != chat_id or action not in {"enable", "disable"}:
            await callback.answer(
                localizer.t("admin.activity.callback.invalid_operation.toast"),
                show_alert=True,
            )
            return

        # 检查是否是管理员
        if callback.from_user.id not in settings.admin_ids:
            if not await PermissionCache.is_admin(bot, chat_id, callback.from_user.id):
                await callback.answer(
                    localizer.t("admin.activity.callback.permission_denied.toast"),
                    show_alert=True,
                )
                return

        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id)

        # 更新设置
        if action == "enable":
            if group.activity_enabled:
                await callback.answer(
                    localizer.t("admin.activity.callback.already_enabled.toast"),
                    show_alert=True,
                )
                return

            await GroupRepository.update_activity_settings(chat_id, True)
            logger.info(f"管理员 {callback.from_user.id} 在群组 {chat_id} 启用了活跃度系统")
            await callback.answer(
                localizer.t("admin.activity.callback.enabled.toast"), show_alert=True
            )
        else:  # action == "disable"
            if not group.activity_enabled:
                await callback.answer(
                    localizer.t("admin.activity.callback.already_disabled.toast"),
                    show_alert=True,
                )
                return

            await GroupRepository.update_activity_settings(chat_id, False)
            logger.info(f"管理员 {callback.from_user.id} 在群组 {chat_id} 禁用了活跃度系统")
            await callback.answer(
                localizer.t("admin.activity.callback.disabled.toast"), show_alert=True
            )

        # 重新获取群组配置,确保状态是最新的
        group = await GroupRepository.get_or_create(chat_id)

        # 更新消息
        text, keyboard = _render_activity_panel(localizer, chat_id, group.activity_enabled)
        await message.edit_text(text, reply_markup=keyboard)

    except ValueError:
        await callback.answer(
            localizer.t("admin.activity.callback.invalid_data.toast"), show_alert=True
        )
    except Exception as e:
        logger.error(f"处理活跃度设置回调失败: {e}")
        await callback.answer(localizer.t("admin.activity.callback.failed.toast"), show_alert=True)


@router.message(Command("activityskip"))
async def cmd_activity_skip(message: Message, bot: Bot, localizer: BoundLocalizer) -> None:
    if not message.from_user:
        return

    """查看/设置活跃度跳过垃圾检测阈值

    用法:
    - /activityskip - 查看当前配置
    - /activityskip <阈值> - 设置群组阈值
    """
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer(localizer.t("admin.activityskip.error.group_only.message"))
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer(localizer.t("admin.activityskip.error.admin_only.message"))
        return

    try:
        # 获取群组配置
        group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

        # 获取全局配置
        global_threshold = settings.activity_skip_spam_check_threshold

        # 解析参数
        if not message.text:
            return

        args = message.text.split()
        if len(args) == 1:
            # 仅查看配置
            await _show_activity_skip_config(message, group, global_threshold, localizer)
        else:
            # 设置阈值
            try:
                new_threshold = int(args[1])
            except ValueError:
                reply = await message.answer(
                    localizer.t("admin.activityskip.validation.not_integer.message")
                )
                await auto_delete_message(reply)
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(f"删除命令消息失败: {e}")
                return

            # 验证范围
            if new_threshold < 0:
                reply = await message.answer(
                    localizer.t("admin.activityskip.validation.negative.message")
                )
                await auto_delete_message(reply)
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(f"删除命令消息失败: {e}")
                return

            # 更新群组配置
            await GroupRepository.update_activity_skip_threshold(message.chat.id, new_threshold)
            # Repository 使用独立 session,不刷新 detached group;同步展示对象避免报告显示旧阈值
            group.activity_skip_threshold = new_threshold

            logger.info(
                f"管理员 {message.from_user.id} 在群组 {message.chat.id} "
                f"设置活跃度跳过阈值为 {new_threshold}"
            )

            # 显示更新后的配置
            await _show_activity_skip_config(
                message, group, global_threshold, localizer, new_value=new_threshold
            )

    except Exception as e:
        logger.error(f"处理活跃度跳过阈值命令失败: {e}")
        reply = await message.answer(localizer.t("admin.activityskip.error.failed.message"))
        await auto_delete_message(reply)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")


async def _show_activity_skip_config(
    message: Message,
    group,
    global_threshold: int,
    localizer: BoundLocalizer,
    new_value: int | None = None,
) -> None:
    """显示活跃度跳过阈值配置信息

    new_value 非 None 表示刚完成设置,在报告头部追加成功提示;
    同时表示 group.activity_skip_threshold 已由调用方同步为新值。
    """
    # 计算有效阈值 + 三态全局覆盖
    if global_threshold > 0:
        effective_threshold = global_threshold
        threshold_source = localizer.t("admin.activityskip.source.global.label")
        global_mode = localizer.t("admin.activityskip.global_mode.uniform.label")
        warning_block = (
            localizer.t(
                "admin.activityskip.warning.global_override.message",
                global_threshold=global_threshold,
            )
            + "\n\n"
        )
    elif global_threshold == 0:
        effective_threshold = group.activity_skip_threshold
        threshold_source = localizer.t("admin.activityskip.source.group.label")
        global_mode = localizer.t("admin.activityskip.global_mode.group.label")
        warning_block = ""
    else:
        effective_threshold = 0
        threshold_source = localizer.t("admin.activityskip.source.disabled.label")
        global_mode = localizer.t("admin.activityskip.global_mode.disabled.label")
        warning_block = localizer.t("admin.activityskip.warning.globally_disabled.message") + "\n\n"

    # 成功块(仅在刚完成设置时渲染)
    if new_value is not None:
        success_block = (
            localizer.t("admin.activityskip.result.saved.message", new_value=new_value) + "\n\n"
        )
    else:
        success_block = ""

    text = localizer.t(
        "admin.activityskip.report.message",
        success_block=success_block,
        global_threshold=global_threshold,
        global_mode=global_mode,
        group_threshold=group.activity_skip_threshold,
        effective_threshold=effective_threshold,
        threshold_source=threshold_source,
        warning_block=warning_block,
    )

    reply = await message.answer(text)
    await auto_delete_message(reply)

    # 删除命令消息
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"删除命令消息失败: {e}")
