"""管理员配置命令处理器"""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from src.core.cache import PermissionCache
from src.core.config import settings
from src.core.health import get_health_checker
from src.core.utils import auto_delete_message, check_admin_permission, escape_html
from src.repositories.group_repo import GroupRepository

router = Router(name="admin")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """处理 /start 命令"""
    await message.answer(
        "🤖 <b>Telegram Guard Bot</b>\n\n"
        "我是一个群管理机器人，支持以下功能：\n\n"
        "🔐 <b>入群验证</b>\n"
        "• /setverify - 设置验证方式\n"
        "• /settimeout - 设置验证超时时间\n"
        "• /verifyconfig - 查看验证配置\n\n"
        "👮 <b>群管理</b>\n"
        "• /kick - 踢出成员\n"
        "• /mute - 禁言成员\n"
        "• /ban - 封禁成员\n"
        "• /warn - 警告成员\n\n"
        "🚨 <b>举报系统</b>\n"
        "• /spam - 举报/标记垃圾消息\n"
        "• /reports - 查看举报列表（管理员）\n"
        "• /approve - 处理举报（管理员）\n\n"
        "🗑️ <b>消息删除</b>\n"
        "• /delbefore - 删除往前的消息\n"
        "• /delafter - 删除往后的消息\n"
        "• /delrange - 删除消息范围\n\n"
        "🛡️ <b>反垃圾</b>\n"
        "• /antispam - 配置反垃圾\n\n"
        "📊 <b>活跃度系统</b>\n"
        "• /activity - 控制活跃度系统开关\n"
        "• /activityskip - 查看/设置活跃度跳过阈值\n\n"
        "💡 <b>提示</b>：将我添加到群组并设为管理员即可使用所有功能"
    )


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
                    text="🤝 Friendly Captcha", callback_data=f"setverify:{message.chat.id}:friendly"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼️ hCaptcha 图片验证", callback_data=f"setverify:{message.chat.id}:hcaptcha"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 MTCaptcha 自适应", callback_data=f"setverify:{message.chat.id}:mtcaptcha"
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
        _, chat_id_str, verify_type = callback.data.split(":")
        chat_id = int(chat_id_str)

        # ✅ 权限验证
        if callback.from_user.id not in settings.admin_ids:
            try:
                member = await callback.bot.get_chat_member(chat_id, callback.from_user.id)
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

        await callback.message.edit_text(
            f"✅ 验证方式已设置为：{verify_type_names.get(verify_type, verify_type)}"
        )
        await callback.answer("设置成功")

        logger.info(f"群组 {chat_id} 的验证方式已更新为 {verify_type}")

    except Exception as e:
        logger.error(f"设置验证方式失败: {e}")
        await callback.answer("❌ 设置失败，请重试", show_alert=True)


@router.message(Command("verifyconfig"))
async def cmd_verify_config(message: Message) -> None:
    """查看验证配置"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    try:
        # 获取群组配置
        group = await GroupRepository.get_or_create(message.chat.id, message.chat.title)

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

        config_text = (
            f"<b>📋 当前验证配置</b>\n\n"
            f"验证方式: {verify_type_names.get(group.verification_type, group.verification_type)}\n"
            f"验证超时: {group.verification_timeout} 秒\n"
            f"反垃圾: {'已启用' if group.antispam_enabled else '已禁用'}\n"
            f"反垃圾级别: {group.antispam_level}/3\n"
            f"活跃度系统: {'已启用' if group.activity_enabled else '已禁用'}"
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
        reply = await message.answer("❌ 获取配置失败，请重试")
        await auto_delete_message(reply)

        # 删除管理员的命令消息
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")


@router.message(Command("settimeout"))
async def cmd_set_timeout(message: Message, bot: Bot) -> None:
    """设置验证超时时间"""
    # 检查是否在群组中
    if message.chat.type == "private":
        await message.answer("❌ 此命令只能在群组中使用")
        return

    # 检查管理员权限
    if not await check_admin_permission(message, bot):
        reply = await message.answer("❌ 只有管理员才能设置验证超时时间")
        await auto_delete_message(reply)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")
        return

    try:
        # 解析超时时间参数
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            reply = await message.answer(
                "❌ 请指定超时时间（秒）\n\n"
                "用法: /settimeout <秒数>\n"
                "范围: 30-300 秒\n"
                "示例: /settimeout 120"
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
            reply = await message.answer("❌ 超时时间必须是数字")
            await auto_delete_message(reply)
            try:
                await message.delete()
            except Exception as e:
                logger.debug(f"删除命令消息失败: {e}")
            return

        # 验证范围
        if not (30 <= timeout <= 300):
            reply = await message.answer(
                "❌ 超时时间必须在 30-300 秒之间\n\n"
                "• 太短可能导致正常用户无法完成验证\n"
                "• 太长可能导致垃圾用户占用资源过久"
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
            f"✅ 已设置验证超时时间为 {timeout} 秒\n\n" "所有新加入的用户将使用此超时时间进行验证。"
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
        reply = await message.answer("❌ 设置失败，请重试")
        await auto_delete_message(reply)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"删除命令消息失败: {e}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """帮助命令"""
    await cmd_start(message)


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    """健康检查命令（仅超级管理员）"""
    # 检查是否是超级管理员
    if message.from_user.id not in settings.admin_ids:
        await message.answer("❌ 只有超级管理员可以使用此命令")
        return

    try:
        health_checker = get_health_checker()
        report = await health_checker.full_check()

        # 构建报告文本
        status_emoji = "✅" if report["healthy"] else "❌"

        text = f"{status_emoji} <b>系统健康状态</b>\n\n"

        # 运行时间
        text += f"⏱️ <b>运行时间</b>: {report['uptime']['formatted']}\n"
        text += f"🔄 <b>检查次数</b>: {report['check_count']}\n\n"

        # 数据库状态
        db = report["database"]
        db_emoji = "✅" if db["healthy"] else "❌"
        text += f"{db_emoji} <b>数据库</b>: {db['latency_ms']:.2f}ms\n"
        if db["error"]:
            text += f"   错误: {db['error']}\n"

        # Redis 状态
        redis = report["redis"]
        redis_emoji = "✅" if redis["healthy"] else "❌"
        text += f"{redis_emoji} <b>Redis</b>: {redis['latency_ms']:.2f}ms\n"
        if redis["error"]:
            text += f"   错误: {redis['error']}\n"

        # 系统资源
        if report.get("system"):
            sys = report["system"]
            text += "\n💻 <b>系统资源</b>\n"
            text += f"• CPU: {sys['cpu']['percent']:.1f}% ({sys['cpu']['count']} 核)\n"
            text += (
                f"• 内存: {sys['memory']['used_mb']:.0f}/{sys['memory']['total_mb']:.0f} MB "
                f"({sys['memory']['percent']:.1f}%)\n"
            )
            text += (
                f"• 磁盘: {sys['disk']['used_gb']:.1f}/{sys['disk']['total_gb']:.1f} GB "
                f"({sys['disk']['percent']:.1f}%)\n"
            )

        await message.answer(text)

    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        # ✅ M2: 不向用户显示详细异常信息，防止信息泄露
        await message.answer("❌ 健康检查失败，请联系管理员")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """统计信息命令（仅超级管理员）"""
    # 检查是否是超级管理员
    if message.from_user.id not in settings.admin_ids:
        await message.answer("❌ 只有超级管理员可以使用此命令")
        return

    try:
        from src.services.spam_detector import get_detector

        # 获取反垃圾统计
        detector = get_detector()
        spam_stats = await detector.get_statistics()

        # 获取警告统计（简单示例）
        # total_warnings = await UserRepository.count_all_warnings()  # 需要实现此方法

        text = "📊 <b>系统统计</b>\n\n"

        # 反垃圾统计
        text += "🛡️ <b>反垃圾系统</b>\n"
        text += f"• 总样本数: {spam_stats.get('total_samples', 0)}\n"
        text += f"• 垃圾样本: {spam_stats.get('spam_samples', 0)}\n"
        text += f"• 正常样本: {spam_stats.get('normal_samples', 0)}\n"
        text += (
            f"• ML 分类器: {'✅ 已训练' if spam_stats.get('classifier_trained') else '❌ 未训练'}\n"
        )
        text += f"• Embedding: {'✅ 已初始化' if spam_stats.get('embedder_initialized') else '❌ 未初始化'}\n"

        # 系统信息
        health_checker = get_health_checker()
        uptime = health_checker.get_uptime()

        text += "\n⏱️ <b>系统信息</b>\n"
        text += f"• 运行时间: {uptime['formatted']}\n"
        text += f"• 启动时间: {uptime['started_at']}\n"

        await message.answer(text)

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        # ✅ M2: 不向用户显示详细异常信息，防止信息泄露
        await message.answer("❌ 获取统计信息失败，请联系管理员")


@router.message(Command("whitelist_add"))
async def cmd_whitelist_add(message: Message) -> None:
    """添加群组到白名单（仅超级管理员）

    用法: /whitelist_add <chat_id> [群组名称]
    示例: /whitelist_add -1001234567890 测试群组
    """
    # 检查是否是超级管理员
    if message.from_user.id not in settings.admin_ids:
        await message.answer("❌ 只有超级管理员可以使用此命令")
        return

    try:
        # 解析参数
        args = message.text.split(maxsplit=2)
        if len(args) < 2:
            await message.answer(
                "❌ 用法错误\n\n"
                "<b>用法</b>: /whitelist_add &lt;chat_id&gt; [群组名称]\n"
                "<b>示例</b>: /whitelist_add -1001234567890 测试群组"
            )
            return

        chat_id = int(args[1])
        title = args[2] if len(args) > 2 else None

        # 获取或创建群组记录
        group = await GroupRepository.get_or_create(chat_id, title)

        if group.is_whitelisted:
            await message.answer(
                f"ℹ️ 群组 <b>{escape_html(group.title) if group.title else chat_id}</b> 已在白名单中"
            )
            return

        # 添加到白名单
        await GroupRepository.update_whitelist(chat_id, True)

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 添加到白名单")
        await message.answer(
            f"✅ 已将群组 <b>{escape_html(group.title) if group.title else chat_id}</b> 添加到白名单"
        )

    except ValueError:
        await message.answer("❌ chat_id 格式错误，必须是数字")
    except Exception as e:
        logger.error(f"添加白名单失败: {e}")
        await message.answer("❌ 添加白名单失败，请重试")


@router.message(Command("whitelist_remove"))
async def cmd_whitelist_remove(message: Message) -> None:
    """从白名单移除群组（仅超级管理员）

    用法: /whitelist_remove <chat_id>
    示例: /whitelist_remove -1001234567890
    """
    # 检查是否是超级管理员
    if message.from_user.id not in settings.admin_ids:
        await message.answer("❌ 只有超级管理员可以使用此命令")
        return

    try:
        # 解析参数
        args = message.text.split()
        if len(args) != 2:
            await message.answer(
                "❌ 用法错误\n\n"
                "<b>用法</b>: /whitelist_remove &lt;chat_id&gt;\n"
                "<b>示例</b>: /whitelist_remove -1001234567890"
            )
            return

        chat_id = int(args[1])

        # 检查群组是否存在
        group = await GroupRepository.get_by_id(chat_id)
        if not group:
            await message.answer(f"❌ 未找到群组 {chat_id}")
            return

        if not group.is_whitelisted:
            # ✅ 安全修复：转义群组标题防止 HTML 注入
            title_safe = escape_html(group.title) if group.title else chat_id
            await message.answer(f"ℹ️ 群组 <b>{title_safe}</b> 不在白名单中")
            return

        # 从白名单移除
        await GroupRepository.update_whitelist(chat_id, False)

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 从白名单移除")
        # ✅ 安全修复：转义群组标题防止 HTML 注入
        title_safe = escape_html(group.title) if group.title else chat_id
        await message.answer(f"✅ 已将群组 <b>{title_safe}</b> 从白名单移除")

    except ValueError:
        await message.answer("❌ chat_id 格式错误，必须是数字")
    except Exception as e:
        logger.error(f"移除白名单失败: {e}")
        await message.answer("❌ 移除白名单失败，请重试")


@router.message(Command("whitelist_list"))
async def cmd_whitelist_list(message: Message) -> None:
    """列出所有白名单群组（仅超级管理员）"""
    # 检查是否是超级管理员
    if message.from_user.id not in settings.admin_ids:
        await message.answer("❌ 只有超级管理员可以使用此命令")
        return

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
            f"<b>说明：</b>\n"
            f"• 启用后，新用户需通过发送文本消息积累活跃度\n"
            f"• 活跃度 > 0 才能发送图片、贴纸、转发等非文本消息\n"
            f"• 发送文本消息 +1，发送非文本消息 -2\n"
            f"• 每日无消息自动衰减 -1"
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
        # 解析回调数据
        _, chat_id_str, action = callback.data.split(":")
        chat_id = int(chat_id_str)

        # 检查权限（回调来自同一用户）
        if callback.message.chat.id != chat_id:
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

            group.activity_enabled = True
            await GroupRepository.update(group)
            logger.info(f"管理员 {callback.from_user.id} 在群组 {chat_id} 启用了活跃度系统")
            await callback.answer("✅ 活跃度系统已启用", show_alert=True)

        elif action == "disable":
            if not group.activity_enabled:
                await callback.answer("ℹ️ 活跃度系统已经是禁用状态", show_alert=True)
                return

            group.activity_enabled = False
            await GroupRepository.update(group)
            logger.info(f"管理员 {callback.from_user.id} 在群组 {chat_id} 禁用了活跃度系统")
            await callback.answer("✅ 活跃度系统已禁用", show_alert=True)

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
            f"<b>说明：</b>\n"
            f"• 启用后，新用户需通过发送文本消息积累活跃度\n"
            f"• 活跃度 > 0 才能发送图片、贴纸、转发等非文本消息\n"
            f"• 发送文本消息 +1，发送非文本消息 -2\n"
            f"• 每日无消息自动衰减 -1"
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except ValueError:
        await callback.answer("❌ 无效的回调数据", show_alert=True)
    except Exception as e:
        logger.error(f"处理活跃度设置回调失败: {e}")
        await callback.answer("❌ 操作失败，请重试", show_alert=True)


@router.message(Command("activityskip"))
async def cmd_activity_skip(message: Message, bot: Bot) -> None:
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
        args = message.text.split()
        if len(args) == 1:
            # 仅查看配置
            await _show_activity_skip_config(message, group, global_threshold)
        else:
            # 设置阈值
            try:
                new_threshold = int(args[1])
            except ValueError:
                reply = await message.answer("❌ 阈值必须是数字（0=禁用，>0=启用）")
                await auto_delete_message(reply)
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(f"删除命令消息失败: {e}")
                return

            # 验证范围
            if new_threshold < 0:
                reply = await message.answer("❌ 阈值不能为负数（0=禁用，>0=启用）")
                await auto_delete_message(reply)
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(f"删除命令消息失败: {e}")
                return

            # 更新群组配置
            group.activity_skip_threshold = new_threshold
            await GroupRepository.update(group)

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
