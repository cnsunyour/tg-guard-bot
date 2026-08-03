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
async def cmd_groupset(message: Message, bot: Bot) -> None:
    """群组设置主菜单（统一配置入口）"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 获取当前配置状态
    try:
        group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

        # 验证方式显示
        verify_type_map = {
            "math": "🔢 数学",
            "slider": "🎯 滑块",
            "qa": "❓ 问答",
            "emoji": "😊 表情",
            "captcha": "🖼️ 图片",
            "honeypot": "🍯 蜜罐",
            "puzzle": "🧩 拼图",
            "turnstile": "🔐 Turnstile",
            "friendly": "🤝 Friendly",
            "hcaptcha": "🖼️ hCaptcha",
            "mtcaptcha": "🔒 MTCaptcha",
            "altcha": "⚡ ALTCHA",
            "random": "🎲 随机",
        }
        verify_text = verify_type_map.get(group.verification_type, "未知")

        antispam_text = "✅" if group.antispam_enabled else "❌"
        antichannel_text = "✅" if group.anti_channel_enabled else "❌"
        activity_text = "✅" if group.activity_enabled else "❌"

    except Exception as e:
        logger.error(f"获取群组配置失败: {e}")
        verify_text = "未知"
        antispam_text = "❌"
        antichannel_text = "❌"
        activity_text = "❌"

    # 显示配置菜单
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 验证方式设置",
                    callback_data=f"groupset_menu:{message.chat.id}:verify",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏱️ 验证超时设置",
                    callback_data=f"groupset_menu:{message.chat.id}:timeout",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛡️ 反垃圾配置",
                    callback_data=f"groupset_menu:{message.chat.id}:antispam",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎭 反频道马甲配置",
                    callback_data=f"groupset_menu:{message.chat.id}:antichannel",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 活跃度系统配置",
                    callback_data=f"groupset_menu:{message.chat.id}:activity",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📈 活跃度跳过阈值",
                    callback_data=f"groupset_menu:{message.chat.id}:activityskip",
                )
            ],
        ]
    )

    reply = await message.answer(
        f"⚙️ <b>群组设置</b>\n\n"
        f"<b>当前配置：</b>\n"
        f"• 验证方式：{verify_text}\n"
        f"• 反垃圾：{antispam_text}\n"
        f"• 反频道马甲：{antichannel_text}\n"
        f"• 活跃度系统：{activity_text}\n\n"
        f"请选择要配置的功能：",
        reply_markup=keyboard,
    )
    await auto_delete_message(reply)


@router.callback_query(F.data.startswith("groupset_menu:"))
async def on_groupset_menu(callback: CallbackQuery, bot: Bot) -> None:
    """处理群组设置菜单回调"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        _, chat_id_str, menu_type = callback.data.split(":")
        chat_id = int(chat_id_str)

        # 权限验证
        if callback.from_user.id not in settings.admin_ids:
            if not await PermissionCache.is_admin(bot, chat_id, callback.from_user.id):
                await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
                return

        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id)

        # 根据菜单类型显示不同的配置界面
        if menu_type == "verify":
            # 验证方式设置
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔢 数学验证", callback_data=f"setverify:{chat_id}:math"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🎯 滑块验证", callback_data=f"setverify:{chat_id}:slider"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❓ 问答验证", callback_data=f"setverify:{chat_id}:qa"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="😊 表情验证", callback_data=f"setverify:{chat_id}:emoji"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🖼️ 图片验证码", callback_data=f"setverify:{chat_id}:captcha"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🍯 蜜罐验证", callback_data=f"setverify:{chat_id}:honeypot"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🧩 拼图验证", callback_data=f"setverify:{chat_id}:puzzle"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔐 Turnstile 验证", callback_data=f"setverify:{chat_id}:turnstile"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🤝 Friendly Captcha",
                            callback_data=f"setverify:{chat_id}:friendly",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🖼️ hCaptcha 图片验证",
                            callback_data=f"setverify:{chat_id}:hcaptcha",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔒 MTCaptcha 自适应",
                            callback_data=f"setverify:{chat_id}:mtcaptcha",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⚡ ALTCHA 工作证明", callback_data=f"setverify:{chat_id}:altcha"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🎲 随机验证", callback_data=f"setverify:{chat_id}:random"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="« 返回主菜单", callback_data=f"groupset_back:{chat_id}"
                        )
                    ],
                ]
            )
            await message.edit_text("请选择验证方式：", reply_markup=keyboard)

        elif menu_type == "timeout":
            # 验证超时设置 - 显示当前配置和设置方法
            timeout = group.verification_timeout or settings.verification_timeout
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="« 返回主菜单", callback_data=f"groupset_back:{chat_id}"
                        )
                    ]
                ]
            )
            await message.edit_text(
                f"⏱️ <b>验证超时设置</b>\n\n"
                f"当前超时时间：{timeout} 秒\n\n"
                f"<b>修改方法：</b>\n"
                f"发送命令：/settimeout &lt;秒数&gt;\n"
                f"范围：30-300 秒\n"
                f"示例：/settimeout 120",
                reply_markup=keyboard,
            )

        elif menu_type == "antispam":
            # 反垃圾配置
            current_status = "✅ 已启用" if group.antispam_enabled else "❌ 已禁用"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ 启用反垃圾", callback_data=f"antispam_toggle:{chat_id}:on"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ 禁用反垃圾", callback_data=f"antispam_toggle:{chat_id}:off"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="« 返回主菜单", callback_data=f"groupset_back:{chat_id}"
                        )
                    ],
                ]
            )
            await message.edit_text(
                f"🛡️ <b>反垃圾配置</b>\n\n"
                f"当前状态: {current_status}\n\n"
                f"💡 <b>说明</b>：\n"
                f"• 启用后，自动检测并删除垃圾消息\n"
                f"• 使用 AI + 规则引擎多层检测\n"
                f"• 可通过 /spam 命令手动标记训练",
                reply_markup=keyboard,
            )

        elif menu_type == "antichannel":
            # 反频道马甲配置
            current_status = "✅ 已启用" if group.anti_channel_enabled else "❌ 已禁用"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ 启用反频道马甲",
                            callback_data=f"antichannel_toggle:{chat_id}:on",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ 禁用反频道马甲",
                            callback_data=f"antichannel_toggle:{chat_id}:off",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="« 返回主菜单", callback_data=f"groupset_back:{chat_id}"
                        )
                    ],
                ]
            )
            await message.edit_text(
                f"🎭 <b>反频道马甲配置</b>\n\n"
                f"当前状态: {current_status}\n\n"
                f"💡 <b>说明</b>：\n"
                f"• 启用后，禁止用户以频道身份发言\n"
                f"• 频道马甲消息会被删除，并记录警告\n"
                f"• 有助于减少广告和频道宣传",
                reply_markup=keyboard,
            )

        elif menu_type == "activity":
            # 活跃度系统配置
            status_text = "已启用 ✅" if group.activity_enabled else "已禁用 ❌"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ 启用活跃度系统",
                            callback_data=f"activity:{chat_id}:enable",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ 禁用活跃度系统",
                            callback_data=f"activity:{chat_id}:disable",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="« 返回主菜单", callback_data=f"groupset_back:{chat_id}"
                        )
                    ],
                ]
            )
            await message.edit_text(
                f"📊 <b>活跃度系统设置</b>\n\n"
                f"当前状态: {status_text}\n\n"
                f"<b>🎯 非文本消息限制：</b>\n"
                f"• <b>启用</b>: 活跃度为 0（从未发言）时禁止发送图片/贴纸/视频等，发一条文本即可解除\n"
                f"• <b>禁用</b>: 不限制非文本消息（新用户也能发）\n\n"
                f"<b>📈 活跃度规则：</b>\n"
                f"• 初始值: 0\n"
                f"• 文本消息: +1\n"
                f"• 非文本消息: 不变（当前不扣分）\n"
                f"• 每日衰减: -1（活跃度 &lt; 10 且当天无消息时，曾发言用户最低保留 1）\n\n"
                f"<b>🛡️ 其他用途（始终生效）：</b>\n"
                f"• 垃圾检测误判修正（活跃度越高，误判率越低）\n"
                f"• 检测豁免阈值（达到阈值可跳过垃圾检测）\n"
                f"• 宵禁模式发言门槛控制",
                reply_markup=keyboard,
            )

        elif menu_type == "activityskip":
            # 活跃度跳过阈值配置
            global_threshold = settings.activity_skip_spam_check_threshold
            group_threshold = group.activity_skip_threshold

            if global_threshold > 0:
                effective_threshold = global_threshold
                threshold_source = "全局配置"
            elif global_threshold == 0:
                effective_threshold = group_threshold
                threshold_source = "群组配置"
            else:
                effective_threshold = 0
                threshold_source = "全局禁用"

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="« 返回主菜单", callback_data=f"groupset_back:{chat_id}"
                        )
                    ]
                ]
            )
            await message.edit_text(
                f"📈 <b>活跃度跳过阈值设置</b>\n\n"
                f"当前配置：\n"
                f"• 群组阈值：{group_threshold}\n"
                f"• 全局阈值：{global_threshold}\n"
                f"• 有效阈值：{effective_threshold}（来自{threshold_source}）\n\n"
                f"<b>说明：</b>\n"
                f"• 活跃度 ≥ 阈值的用户跳过反垃圾检测\n"
                f"• 设为 0 表示禁用此功能\n\n"
                f"<b>修改方法：</b>\n"
                f"发送命令：/activityskip &lt;阈值&gt;\n"
                f"示例：/activityskip 10",
                reply_markup=keyboard,
            )

        await callback.answer()

    except Exception as e:
        logger.error(f"处理群组设置菜单回调失败: {e}")
        await callback.answer("❌ 操作失败", show_alert=True)


@router.callback_query(F.data.startswith("groupset_back:"))
async def on_groupset_back(callback: CallbackQuery, bot: Bot) -> None:
    """返回群组设置主菜单"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        _, chat_id_str = callback.data.split(":")
        chat_id = int(chat_id_str)

        # 权限验证
        if callback.from_user.id not in settings.admin_ids:
            if not await PermissionCache.is_admin(bot, chat_id, callback.from_user.id):
                await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
                return

        # 获取当前配置状态
        group = await GroupRepository.get_or_create(chat_id)

        # 验证方式显示
        verify_type_map = {
            "math": "🔢 数学",
            "slider": "🎯 滑块",
            "qa": "❓ 问答",
            "emoji": "😊 表情",
            "captcha": "🖼️ 图片",
            "honeypot": "🍯 蜜罐",
            "puzzle": "🧩 拼图",
            "turnstile": "🔐 Turnstile",
            "friendly": "🤝 Friendly",
            "hcaptcha": "🖼️ hCaptcha",
            "mtcaptcha": "🔒 MTCaptcha",
            "altcha": "⚡ ALTCHA",
            "random": "🎲 随机",
        }
        verify_text = verify_type_map.get(group.verification_type, "未知")

        antispam_text = "✅" if group.antispam_enabled else "❌"
        antichannel_text = "✅" if group.anti_channel_enabled else "❌"
        activity_text = "✅" if group.activity_enabled else "❌"

        # 显示主菜单
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔐 验证方式设置",
                        callback_data=f"groupset_menu:{chat_id}:verify",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⏱️ 验证超时设置",
                        callback_data=f"groupset_menu:{chat_id}:timeout",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🛡️ 反垃圾配置",
                        callback_data=f"groupset_menu:{chat_id}:antispam",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🎭 反频道马甲配置",
                        callback_data=f"groupset_menu:{chat_id}:antichannel",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📊 活跃度系统配置",
                        callback_data=f"groupset_menu:{chat_id}:activity",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📈 活跃度跳过阈值",
                        callback_data=f"groupset_menu:{chat_id}:activityskip",
                    )
                ],
            ]
        )

        await message.edit_text(
            f"⚙️ <b>群组设置</b>\n\n"
            f"<b>当前配置：</b>\n"
            f"• 验证方式：{verify_text}\n"
            f"• 反垃圾：{antispam_text}\n"
            f"• 反频道马甲：{antichannel_text}\n"
            f"• 活跃度系统：{activity_text}\n\n"
            f"请选择要配置的功能：",
            reply_markup=keyboard,
        )
        await callback.answer()

    except Exception as e:
        logger.error(f"返回群组设置主菜单失败: {e}")
        await callback.answer("❌ 操作失败", show_alert=True)


@router.message(Command("setverify"))
async def cmd_set_verify(message: Message, bot: Bot) -> None:
    """设置验证方式"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限（使用统一的权限检查函数）
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 显示验证方式选择
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔢 数学验证", callback_data=f"setverify:{message.chat.id}:math"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 滑块验证", callback_data=f"setverify:{message.chat.id}:slider"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❓ 问答验证", callback_data=f"setverify:{message.chat.id}:qa"
                )
            ],
            [
                InlineKeyboardButton(
                    text="😊 表情验证", callback_data=f"setverify:{message.chat.id}:emoji"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼️ 图片验证码", callback_data=f"setverify:{message.chat.id}:captcha"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🍯 蜜罐验证", callback_data=f"setverify:{message.chat.id}:honeypot"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧩 拼图验证", callback_data=f"setverify:{message.chat.id}:puzzle"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔐 Turnstile 验证", callback_data=f"setverify:{message.chat.id}:turnstile"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤝 Friendly Captcha",
                    callback_data=f"setverify:{message.chat.id}:friendly",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼️ hCaptcha 图片验证",
                    callback_data=f"setverify:{message.chat.id}:hcaptcha",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 MTCaptcha 自适应",
                    callback_data=f"setverify:{message.chat.id}:mtcaptcha",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚡ ALTCHA 工作证明", callback_data=f"setverify:{message.chat.id}:altcha"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎲 随机验证", callback_data=f"setverify:{message.chat.id}:random"
                )
            ],
        ]
    )

    reply = await message.answer("请选择验证方式：", reply_markup=keyboard)
    await auto_delete_message(reply)


@router.callback_query(F.data.startswith("setverify:"))
async def on_setverify_callback(callback: CallbackQuery) -> None:
    """处理验证方式设置回调"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        _, chat_id_str, verify_type = callback.data.split(":")
        chat_id = int(chat_id_str)

        # ✅ 权限验证
        if callback.from_user.id not in settings.admin_ids:
            try:
                member = await callback.bot.get_chat_member(chat_id, callback.from_user.id)  # type: ignore[union-attr]
                if member.status not in ["creator", "administrator"]:
                    await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
                    logger.warning(
                        f"用户 {callback.from_user.id} 尝试修改群组 {chat_id} 设置但无权限"
                    )
                    return
            except Exception as e:
                logger.error(f"权限检查失败: {e}")
                await callback.answer("❌ 权限验证失败", show_alert=True)
                return

        # ✅ 参数白名单验证
        if verify_type not in [
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
        ]:
            await callback.answer("❌ 无效的验证类型", show_alert=True)
            logger.warning(f"无效的验证类型: {verify_type}")
            return

        # 更新验证方式
        await GroupRepository.update_verification_type(chat_id, verify_type)

        verify_type_names = {
            "math": "数学验证",
            "slider": "滑块验证",
            "qa": "问答验证",
            "emoji": "表情验证",
            "captcha": "图片验证码",
            "honeypot": "蜜罐验证",
            "puzzle": "拼图验证",
            "turnstile": "Turnstile 验证",
            "friendly": "Friendly Captcha",
            "hcaptcha": "hCaptcha 图片验证",
            "mtcaptcha": "MTCaptcha 自适应",
            "altcha": "ALTCHA 工作证明",
            "random": "随机验证",
        }

        await message.edit_text(
            f"✅ 验证方式已设置为：{verify_type_names.get(verify_type, verify_type)}"
        )
        await callback.answer("设置成功")

        logger.info(f"群组 {chat_id} 的验证方式已更新为 {verify_type}")

    except Exception as e:
        logger.error(f"设置验证方式失败: {e}")
        await callback.answer("❌ 设置失败，请重试", show_alert=True)


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


async def _list_whitelist(message: Message) -> None:
    """列出所有白名单群组"""
    try:
        # 获取所有白名单群组
        groups = await GroupRepository.get_whitelisted_groups()

        if not groups:
            await message.answer("📋 当前没有白名单群组")
            return

        text = f"📋 <b>白名单群组列表</b> (共 {len(groups)} 个)\n\n"

        for i, group in enumerate(groups, 1):
            title = escape_html(group.title) if group.title else "未知群组"
            text += f"{i}. <b>{title}</b>\n"
            text += f"   ID: <code>{group.id}</code>\n"
            if i < len(groups):
                text += "\n"

        await message.answer(text)

    except Exception as e:
        logger.error(f"获取白名单列表失败: {e}")
        await message.answer("❌ 获取白名单列表失败，请重试")


async def _add_whitelist(message: Message, args: list[str]) -> None:
    """添加群组到白名单"""
    try:
        # 检查参数
        if len(args) < 3:
            await message.answer(
                "❌ 用法错误\n\n"
                "<b>用法</b>: /whitelist add &lt;chat_id&gt; [群组名称]\n"
                "<b>示例</b>: /whitelist add -1001234567890 测试群组"
            )
            return

        chat_id = int(args[2])
        title = args[3] if len(args) > 3 else None

        # 获取或创建群组记录
        group = await GroupRepository.get_or_create(chat_id, title)

        if group.is_whitelisted:
            await message.answer(
                f"ℹ️ 群组 <b>{escape_html(group.title) if group.title else chat_id}</b> 已在白名单中"
            )
            return

        # 添加到白名单
        await GroupRepository.update_whitelist(chat_id, True)

        if not message.from_user:
            return

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 添加到白名单")
        await message.answer(
            f"✅ 已将群组 <b>{escape_html(group.title) if group.title else chat_id}</b> 添加到白名单"
        )

    except ValueError:
        await message.answer("❌ chat_id 格式错误，必须是数字")
    except Exception as e:
        logger.error(f"添加白名单失败: {e}")
        await message.answer("❌ 添加白名单失败，请重试")


async def _remove_whitelist(message: Message, args: list[str]) -> None:
    """从白名单移除群组"""
    try:
        # 检查参数
        if len(args) != 3:
            await message.answer(
                "❌ 用法错误\n\n"
                "<b>用法</b>: /whitelist remove &lt;chat_id&gt;\n"
                "<b>示例</b>: /whitelist remove -1001234567890"
            )
            return

        chat_id = int(args[2])

        # 检查群组是否存在
        group = await GroupRepository.get_by_id(chat_id)
        if not group:
            await message.answer(f"❌ 未找到群组 {chat_id}")
            return

        if not group.is_whitelisted:
            title_safe = escape_html(group.title) if group.title else chat_id
            await message.answer(f"ℹ️ 群组 <b>{title_safe}</b> 不在白名单中")
            return

        # 从白名单移除
        await GroupRepository.update_whitelist(chat_id, False)

        if not message.from_user:
            return

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 从白名单移除")
        title_safe = escape_html(group.title) if group.title else chat_id
        await message.answer(f"✅ 已将群组 <b>{title_safe}</b> 从白名单移除")

    except ValueError:
        await message.answer("❌ chat_id 格式错误，必须是数字")
    except Exception as e:
        logger.error(f"移除白名单失败: {e}")
        await message.answer("❌ 移除白名单失败，请重试")


@router.message(Command("whitelist"))
async def cmd_whitelist(message: Message) -> None:
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
        await message.answer("❌ 只有超级管理员可以使用此命令")
        return

    if not message.text:
        return

    args = message.text.split(maxsplit=3)

    # 无参数 - 列出白名单
    if len(args) == 1:
        await _list_whitelist(message)
        return

    subcommand = args[1].lower()

    # add 子命令
    if subcommand == "add":
        await _add_whitelist(message, args)
        return

    # remove 子命令
    if subcommand == "remove":
        await _remove_whitelist(message, args)
        return

    # 未知子命令
    await message.answer(
        "❌ 未知子命令\n\n"
        "<b>用法</b>:\n"
        "• /whitelist - 列出所有白名单群组\n"
        "• /whitelist add &lt;chat_id&gt; [群组名称] - 添加群组到白名单\n"
        "• /whitelist remove &lt;chat_id&gt; - 从白名单移除群组"
    )


@router.message(Command("activity"))
async def cmd_activity(message: Message, bot: Bot) -> None:
    """控制群组活跃度系统开关"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
        return

    # 显示活跃度控制面板
    try:
        group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

        status_text = "已启用 ✅" if group.activity_enabled else "已禁用 ❌"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ 启用活跃度系统",
                        callback_data=f"activity:{message.chat.id}:enable",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ 禁用活跃度系统",
                        callback_data=f"activity:{message.chat.id}:disable",
                    )
                ],
            ]
        )

        text = (
            f"<b>📊 活跃度系统设置</b>\n\n"
            f"当前状态: {status_text}\n\n"
            f"<b>🎯 非文本消息限制：</b>\n"
            f"• <b>启用</b>: 活跃度为 0（从未发言）时禁止发送图片/贴纸/视频等，发一条文本即可解除\n"
            f"• <b>禁用</b>: 不限制非文本消息（新用户也能发）\n\n"
            f"<b>📈 活跃度规则：</b>\n"
            f"• 初始值: 0\n"
            f"• 文本消息: +1\n"
            f"• 非文本消息: 不变（当前不扣分）\n"
            f"• 每日衰减: -1（活跃度 &lt; 10 且当天无消息时，曾发言用户最低保留 1）\n\n"
            f"<b>🛡️ 其他用途（始终生效）：</b>\n"
            f"• 垃圾检测误判修正（活跃度越高，误判率越低）\n"
            f"• 检测豁免阈值（达到阈值可跳过垃圾检测）\n"
            f"• 宵禁模式发言门槛控制"
        )

        reply = await message.answer(text, reply_markup=keyboard)
        await auto_delete_message(reply)

        # 删除管理员的命令消息
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")

    except Exception as e:
        logger.error(f"获取群组配置失败: {e}")
        await message.answer("❌ 获取配置失败，请重试")


@router.callback_query(F.data.startswith("activity:"))
async def on_activity_callback(callback: CallbackQuery, bot: Bot) -> None:
    """处理活跃度设置回调"""
    try:
        # 类型检查
        if not callback.data or not callback.message:
            await callback.answer("❌ 数据错误", show_alert=True)
            return

        # 类型缩小：确保 message 不是 InaccessibleMessage
        from aiogram.types import InaccessibleMessage, Message

        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("❌ 消息不可访问", show_alert=True)
            return

        message: Message = callback.message

        # 解析回调数据
        _, chat_id_str, action = callback.data.split(":")
        chat_id = int(chat_id_str)

        # 检查权限（回调来自同一用户）
        if message.chat.id != chat_id:
            await callback.answer("❌ 无效的操作", show_alert=True)
            return

        # 检查是否是管理员
        if callback.from_user.id not in settings.admin_ids:
            if not await PermissionCache.is_admin(bot, chat_id, callback.from_user.id):
                await callback.answer("❌ 只有管理员可以修改设置", show_alert=True)
                return

        # 获取群组配置
        group = await GroupRepository.get_or_create(chat_id)

        # 更新设置
        if action == "enable":
            if group.activity_enabled:
                await callback.answer("ℹ️ 活跃度系统已经是启用状态", show_alert=True)
                return

            await GroupRepository.update_activity_settings(chat_id, True)
            logger.info(f"管理员 {callback.from_user.id} 在群组 {chat_id} 启用了活跃度系统")
            await callback.answer("✅ 活跃度系统已启用", show_alert=True)

        elif action == "disable":
            if not group.activity_enabled:
                await callback.answer("ℹ️ 活跃度系统已经是禁用状态", show_alert=True)
                return

            await GroupRepository.update_activity_settings(chat_id, False)
            logger.info(f"管理员 {callback.from_user.id} 在群组 {chat_id} 禁用了活跃度系统")
            await callback.answer("✅ 活跃度系统已禁用", show_alert=True)

        # 重新获取群组配置，确保状态是最新的
        group = await GroupRepository.get_or_create(chat_id)

        # 更新消息
        status_text = "已启用 ✅" if group.activity_enabled else "已禁用 ❌"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ 启用活跃度系统",
                        callback_data=f"activity:{chat_id}:enable",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ 禁用活跃度系统",
                        callback_data=f"activity:{chat_id}:disable",
                    )
                ],
            ]
        )

        text = (
            f"<b>📊 活跃度系统设置</b>\n\n"
            f"当前状态: {status_text}\n\n"
            f"<b>🎯 非文本消息限制：</b>\n"
            f"• <b>启用</b>: 活跃度为 0（从未发言）时禁止发送图片/贴纸/视频等，发一条文本即可解除\n"
            f"• <b>禁用</b>: 不限制非文本消息（新用户也能发）\n\n"
            f"<b>📈 活跃度规则：</b>\n"
            f"• 初始值: 0\n"
            f"• 文本消息: +1\n"
            f"• 非文本消息: 不变（当前不扣分）\n"
            f"• 每日衰减: -1（活跃度 &lt; 10 且当天无消息时，曾发言用户最低保留 1）\n\n"
            f"<b>🛡️ 其他用途（始终生效）：</b>\n"
            f"• 垃圾检测误判修正（活跃度越高，误判率越低）\n"
            f"• 检测豁免阈值（达到阈值可跳过垃圾检测）\n"
            f"• 宵禁模式发言门槛控制"
        )

        await message.edit_text(text, reply_markup=keyboard)

    except ValueError:
        await callback.answer("❌ 无效的回调数据", show_alert=True)
    except Exception as e:
        logger.error(f"处理活跃度设置回调失败: {e}")
        await callback.answer("❌ 操作失败，请重试", show_alert=True)


@router.message(Command("activityskip"))
async def cmd_activity_skip(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    """查看/设置活跃度跳过垃圾检测阈值

    用法:
    - /activityskip - 查看当前配置
    - /activityskip <阈值> - 设置群组阈值
    """
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查权限
    if not await check_admin_permission(message, bot):
        await message.answer("❌ 只有管理员可以使用此命令")
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
            await _show_activity_skip_config(message, group, global_threshold)
        else:
            # 设置阈值
            try:
                new_threshold = int(args[1])
            except ValueError:
                reply = await message.answer("❌ 阈值必须是数字（0=禁用，&gt;0=启用）")
                await auto_delete_message(reply)
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(f"删除命令消息失败: {e}")
                return

            # 验证范围
            if new_threshold < 0:
                reply = await message.answer("❌ 阈值不能为负数（0=禁用，&gt;0=启用）")
                await auto_delete_message(reply)
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(f"删除命令消息失败: {e}")
                return

            # 更新群组配置
            await GroupRepository.update_activity_skip_threshold(message.chat.id, new_threshold)
            await GroupRepository.update_activity_skip_threshold(message.chat.id, new_threshold)

            logger.info(
                f"管理员 {message.from_user.id} 在群组 {message.chat.id} "
                f"设置活跃度跳过阈值为 {new_threshold}"
            )

            # 显示更新后的配置
            await _show_activity_skip_config(
                message, group, global_threshold, show_success=True, new_value=new_threshold
            )

    except Exception as e:
        logger.error(f"处理活跃度跳过阈值命令失败: {e}")
        reply = await message.answer("❌ 操作失败，请重试")
        await auto_delete_message(reply)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")


async def _show_activity_skip_config(
    message: Message,
    group,
    global_threshold: int,
    show_success: bool = False,
    new_value: int | None = None,
) -> None:
    """显示活跃度跳过阈值配置信息"""
    # 计算有效阈值
    if global_threshold > 0:
        effective_threshold = global_threshold
        threshold_source = "全局配置"
        warning = f"⚠️ <b>警告</b>：全局配置生效（阈值 = {global_threshold}），群组配置被覆盖"
    elif global_threshold == 0:
        effective_threshold = group.activity_skip_threshold
        threshold_source = "群组配置"
        warning = None
    else:
        effective_threshold = 0
        threshold_source = "全局禁用"
        warning = "⚠️ <b>警告</b>：全局禁用活跃度跳过检测，群组配置无效"

    # 构建消息文本
    text = "<b>📊 活跃度跳过垃圾检测阈值</b>\n\n"

    if show_success and new_value is not None:
        text += f"✅ 群组阈值已设置为 <b>{new_value}</b>\n\n"

    text += "<b>当前配置：</b>\n"
    text += f"• 全局阈值: {global_threshold}"
    if global_threshold > 0:
        text += " (全局统一)\n"
    elif global_threshold == 0:
        text += " (使用群组配置)\n"
    else:
        text += " (全局禁用)\n"

    text += f"• 群组阈值: {group.activity_skip_threshold}\n"
    text += f"• 有效阈值: <b>{effective_threshold}</b> (来源: {threshold_source})\n\n"

    if warning:
        text += f"{warning}\n\n"

    text += (
        "<b>功能说明：</b>\n"
        "• 当用户活跃度 ≥ 有效阈值时，跳过垃圾检测\n"
        "• 设置为 0 表示禁用此功能\n"
        "• 建议阈值：50-200（根据群组活跃度调整）\n\n"
        "<b>配置优先级：</b>\n"
        "• 全局阈值 &gt; 0：使用全局配置（所有群组统一）\n"
        "• 全局阈值 = 0：使用群组配置（每个群组独立）\n"
        "• 全局阈值 &lt; 0：全局禁用（所有群组都不跳过）\n\n"
        "<b>用法：</b>\n"
        "• /activityskip - 查看当前配置\n"
        "• /activityskip <code>[阈值]</code> - 设置群组阈值"
    )

    reply = await message.answer(text)
    await auto_delete_message(reply)

    # 删除命令消息
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"删除命令消息失败: {e}")
