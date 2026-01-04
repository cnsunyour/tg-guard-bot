"""管理员配置命令处理器"""

from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from loguru import logger

from src.repositories.group_repo import GroupRepository
from src.core.config import settings
from src.core.health import get_health_checker
from src.core.utils import auto_delete_message, check_admin_permission

router = Router(name="admin")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """处理 /start 命令"""
    await message.answer(
        "🤖 <b>Telegram Guard Bot</b>\n\n"
        "我是一个群管理机器人，支持以下功能：\n\n"
        "🔐 <b>入群验证</b>\n"
        "• /setverify - 设置验证方式\n"
        "• /verifyconfig - 查看验证配置\n\n"
        "👮 <b>群管理</b>\n"
        "• /kick - 踢出成员\n"
        "• /mute - 禁言成员\n"
        "• /ban - 封禁成员\n"
        "• /warn - 警告成员\n\n"
        "🗑️ <b>消息删除</b>\n"
        "• /delbefore - 删除往前的消息\n"
        "• /delafter - 删除往后的消息\n"
        "• /delrange - 删除消息范围\n\n"
        "🛡️ <b>反垃圾</b>\n"
        "• /antispam - 配置反垃圾\n\n"
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
                    text="🔘 按钮验证", callback_data=f"setverify:{message.chat.id}:button"
                )
            ],
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
                member = await callback.bot.get_chat_member(
                    chat_id,
                    callback.from_user.id
                )
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
        if verify_type not in ["button", "math", "slider"]:
            await callback.answer("❌ 无效的验证类型", show_alert=True)
            logger.warning(f"无效的验证类型: {verify_type}")
            return

        # 更新验证方式
        await GroupRepository.update_verification_type(chat_id, verify_type)

        verify_type_names = {"button": "按钮验证", "math": "数学验证", "slider": "滑块验证"}

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

        verify_type_names = {"button": "按钮验证", "math": "数学验证", "slider": "滑块验证"}

        config_text = (
            f"<b>📋 当前验证配置</b>\n\n"
            f"验证方式: {verify_type_names.get(group.verification_type, group.verification_type)}\n"
            f"验证超时: {group.verification_timeout} 秒\n"
            f"反垃圾: {'已启用' if group.antispam_enabled else '已禁用'}\n"
            f"反垃圾级别: {group.antispam_level}/3"
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
        if "system" in report and report["system"]:
            sys = report["system"]
            text += f"\n💻 <b>系统资源</b>\n"
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
        from src.repositories.spam_repo import SpamRepository
        from src.repositories.user_repo import UserRepository
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
        text += (
            f"• Embedding: {'✅ 已初始化' if spam_stats.get('embedder_initialized') else '❌ 未初始化'}\n"
        )

        # 系统信息
        health_checker = get_health_checker()
        uptime = health_checker.get_uptime()

        text += f"\n⏱️ <b>系统信息</b>\n"
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
            await message.answer(f"ℹ️ 群组 <b>{group.title or chat_id}</b> 已在白名单中")
            return

        # 添加到白名单
        await GroupRepository.update_whitelist(chat_id, True)

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 添加到白名单")
        await message.answer(f"✅ 已将群组 <b>{group.title or chat_id}</b> 添加到白名单")

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
            await message.answer(f"ℹ️ 群组 <b>{group.title or chat_id}</b> 不在白名单中")
            return

        # 从白名单移除
        await GroupRepository.update_whitelist(chat_id, False)

        logger.info(f"超级管理员 {message.from_user.id} 将群组 {chat_id} 从白名单移除")
        await message.answer(f"✅ 已将群组 <b>{group.title or chat_id}</b> 从白名单移除")

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
            text += f"{i}. <b>{group.title or '未知群组'}</b>\n"
            text += f"   ID: <code>{group.id}</code>\n"
            if i < len(groups):
                text += "\n"

        await message.answer(text)

    except Exception as e:
        logger.error(f"获取白名单列表失败: {e}")
        await message.answer("❌ 获取白名单列表失败，请重试")

