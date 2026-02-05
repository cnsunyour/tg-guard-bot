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
        "⚙️ <b>群组配置</b>\n"
        "• /groupset - 群组设置（统一入口）\n"
        "• /verifyconfig - 查看验证配置\n\n"
        "👮 <b>群管理</b>\n"
        "• /kick - 踢出成员\n"
        "• /mute - 禁言成员\n"
        "• /ban - 封禁成员\n"
        "• /warn - 警告成员\n"
        "• /cleanup - 清理不活跃用户（安全模式）\n\n"
        "🚨 <b>举报系统</b>\n"
        "• /spam 或 /report - 举报/标记垃圾消息\n"
        "• /reports - 查看举报列表（管理员）\n"
        "• /approve - 接受举报（管理员）\n"
        "• /reject - 拒绝举报（管理员）\n\n"
        "🗑️ <b>消息删除</b>\n"
        "• /delbefore - 删除往前的消息\n"
        "• /delafter - 删除往后的消息\n"
        "• /delrange - 删除消息范围\n\n"
        "💡 <b>使用提示</b>\n"
        "1️⃣ 联系超级管理员将群组加入白名单\n"
        "2️⃣ 将 Bot 添加到群组并设为管理员\n"
        "3️⃣ 使用 /help &lt;命令&gt; 查看命令详细用法\n"
        "   示例：/help groupset"
    )


# 命令详细帮助文档
COMMAND_HELP = {
    "groupset": (
        "⚙️ <b>/groupset - 群组设置（统一入口）</b>\n\n"
        "<b>功能说明：</b>\n"
        "打开群组配置主菜单，集中管理所有群组设置。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/groupset\n"
        "• 点击按钮选择要配置的功能\n\n"
        "<b>可配置项：</b>\n"
        "• 验证方式（数学/滑块/问答等12种）\n"
        "• 验证超时时间\n"
        "• 反垃圾开关\n"
        "• 反频道马甲开关\n"
        "• 活跃度系统开关\n"
        "• 活跃度跳过阈值\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "setverify": (
        "🔐 <b>/setverify - 设置验证方式</b>\n\n"
        "<b>功能说明：</b>\n"
        "设置新用户入群时的验证方式。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/setverify\n"
        "• 选择验证方式（12种可选）\n\n"
        "<b>验证方式：</b>\n"
        "• 🔢 数学验证 - 简单算术题\n"
        "• 🎯 滑块验证 - 滑动验证\n"
        "• ❓ 问答验证 - 自定义问题\n"
        "• 😊 表情验证 - 选择表情\n"
        "• 🖼️ 图片验证码 - 图片识别\n"
        "• 🍯 蜜罐验证 - 隐藏字段\n"
        "• 🧩 拼图验证 - 拼图游戏\n"
        "• 🔐 Turnstile - Cloudflare\n"
        "• 🤝 Friendly - 友好验证\n"
        "• 🖼️ hCaptcha - 图片验证\n"
        "• 🔒 MTCaptcha - 自适应\n"
        "• ⚡ ALTCHA - 工作证明\n"
        "• 🎲 随机验证 - 随机选择\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "settimeout": (
        "⏱️ <b>/settimeout - 设置验证超时时间</b>\n\n"
        "<b>功能说明：</b>\n"
        "设置新用户完成验证的超时时间。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/settimeout &lt;秒数&gt;\n"
        "• 仅发送 /settimeout 查看当前配置\n\n"
        "<b>使用示例：</b>\n"
        "• /settimeout 120 - 设置为2分钟\n"
        "• /settimeout 300 - 设置为5分钟\n\n"
        "<b>参数范围：</b>\n"
        "• 最小：30 秒\n"
        "• 最大：300 秒（5分钟）\n"
        "• 默认：120 秒（2分钟）\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "verifyconfig": (
        "📋 <b>/verifyconfig - 查看验证配置</b>\n\n"
        "<b>功能说明：</b>\n"
        "查看当前群组的验证配置信息。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/verifyconfig\n\n"
        "<b>显示内容：</b>\n"
        "• 当前验证方式\n"
        "• 验证超时时间\n"
        "• 配置来源（群组/全局）\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "antispam": (
        "🛡️ <b>/antispam - 反垃圾配置</b>\n\n"
        "<b>功能说明：</b>\n"
        "开启或关闭群组的反垃圾消息功能。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/antispam\n"
        "• 点击按钮启用或禁用\n\n"
        "<b>检测机制：</b>\n"
        "• 规则引擎 - 关键词/URL/频率检测\n"
        "• ML 分类器 - TF-IDF + SVM\n"
        "• 语义分析 - Embedding 相似度\n\n"
        "<b>训练方式：</b>\n"
        "• 使用 /spam 命令标记垃圾消息\n"
        "• 使用 /notspam 取消误判\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "antichannel": (
        "🎭 <b>/antichannel - 反频道马甲配置</b>\n\n"
        "<b>功能说明：</b>\n"
        "禁止用户以频道身份在群组发言。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/antichannel\n"
        "• 点击按钮启用或禁用\n\n"
        "<b>工作原理：</b>\n"
        "• 检测消息发送者身份\n"
        "• 如果是频道身份，删除消息\n"
        "• 记录警告次数\n\n"
        "<b>适用场景：</b>\n"
        "• 减少频道广告\n"
        "• 防止频道宣传\n"
        "• 保持群组氛围\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "activity": (
        "📊 <b>/activity - 活跃度系统开关</b>\n\n"
        "<b>功能说明：</b>\n"
        "控制群组活跃度系统的开启和关闭。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/activity\n"
        "• 点击按钮启用或禁用\n\n"
        "<b>活跃度规则：</b>\n"
        "• 发送文本消息 +1\n"
        "• 发送非文本消息 -2\n"
        "• 每日无消息 -1\n"
        "• 活跃度 > 0 才能发非文本消息\n\n"
        "<b>作用：</b>\n"
        "• 防止新用户立即发广告\n"
        "• 鼓励正常交流\n"
        "• 减少垃圾信息\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "activityskip": (
        "📈 <b>/activityskip - 活跃度跳过阈值</b>\n\n"
        "<b>功能说明：</b>\n"
        "设置活跃度达到多少时跳过反垃圾检测。\n\n"
        "<b>使用方法：</b>\n"
        "• 查看配置：/activityskip\n"
        "• 设置阈值：/activityskip &lt;数值&gt;\n\n"
        "<b>使用示例：</b>\n"
        "• /activityskip 10 - 活跃度≥10跳过检测\n"
        "• /activityskip 0 - 禁用此功能\n\n"
        "<b>配置优先级：</b>\n"
        "• 全局配置 > 0 → 使用全局配置\n"
        "• 全局配置 = 0 → 使用群组配置\n"
        "• 全局配置 < 0 → 全局禁用\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "kick": (
        "👢 <b>/kick - 踢出成员</b>\n\n"
        "<b>功能说明：</b>\n"
        "将用户踢出群组（可重新加入）。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复用户消息：/kick [原因]\n"
        "• 指定用户ID：/kick &lt;user_id&gt; [原因]\n"
        "• 选择用户：/kick @用户 [原因]（输入 @ 从列表选择）\n"
        "• 删除所有消息：/kick -d [原因]\n\n"
        "<b>使用示例：</b>\n"
        "• /kick 违反群规\n"
        "• /kick 123456789 发送广告\n"
        "• /kick -d 垃圾广告 - 踢出并删除该用户的所有消息\n\n"
        "<b>-d 参数说明：</b>\n"
        "• 添加 -d 参数可以删除该用户的所有历史消息\n"
        "• 适用于垃圾广告用户的快速清理\n\n"
        "<b>与 ban 的区别：</b>\n"
        "• kick - 用户可重新加入\n"
        "• ban - 用户被永久封禁\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "mute": (
        "🔇 <b>/mute - 禁言成员</b>\n\n"
        "<b>功能说明：</b>\n"
        "禁止用户在群组发送消息。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复用户消息：/mute [时长] [原因]\n"
        "• 指定用户ID：/mute &lt;user_id&gt; [时长] [原因]\n"
        "• 选择用户：/mute @用户 [时长] [原因]（输入 @ 从列表选择）\n\n"
        "<b>时长格式：</b>\n"
        "• 分钟：5m, 10m, 30m\n"
        "• 小时：1h, 2h, 24h\n"
        "• 天数：1d, 7d, 30d\n"
        "• 永久：forever 或不指定时长\n\n"
        "<b>使用示例：</b>\n"
        "• /mute 10m 刷屏\n"
        "• /mute 1d 辱骂他人\n"
        "• /mute forever 严重违规\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "unmute": (
        "🔊 <b>/unmute - 解除禁言/封禁</b>\n\n"
        "<b>功能说明：</b>\n"
        "解除用户的禁言或封禁状态。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复用户消息：/unmute\n"
        "• 指定用户ID：/unmute &lt;user_id&gt;\n"
        "• 选择用户：/unmute @用户（输入 @ 从列表选择）\n\n"
        "<b>使用示例：</b>\n"
        "• /unmute\n"
        "• /unmute 123456789\n\n"
        "<b>注意事项：</b>\n"
        "• 可解除 Bot 设置的禁言和封禁\n"
        "• /unmute 和 /unban 功能完全相同\n"
        "• 无法解除 Telegram 原生的限制\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "ban": (
        "🚫 <b>/ban - 封禁成员</b>\n\n"
        "<b>功能说明：</b>\n"
        "永久封禁用户，禁止加入群组。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复用户消息：/ban [原因]\n"
        "• 指定用户ID：/ban &lt;user_id&gt; [原因]\n"
        "• 选择用户：/ban @用户 [原因]（输入 @ 从列表选择）\n"
        "• 删除所有消息：/ban -d [原因]\n\n"
        "<b>使用示例：</b>\n"
        "• /ban 多次违规\n"
        "• /ban 123456789 恶意广告\n"
        "• /ban -d 垃圾广告 - 封禁并删除该用户的所有消息\n\n"
        "<b>-d 参数说明：</b>\n"
        "• 添加 -d 参数可以删除该用户的所有历史消息\n"
        "• 适用于垃圾广告用户的彻底清理\n\n"
        "<b>与 kick 的区别：</b>\n"
        "• ban - 永久封禁，无法重新加入\n"
        "• kick - 踢出后可重新加入\n\n"
        "<b>解除封禁：</b>\n"
        "• 使用 /unban 命令\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "unban": (
        "✅ <b>/unban - 解除封禁</b>\n\n"
        "<b>功能说明：</b>\n"
        "解除用户的封禁状态。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复用户消息：/unban\n"
        "• 指定用户ID：/unban &lt;user_id&gt;\n"
        "• 选择用户：/unban @用户（输入 @ 从列表选择）\n\n"
        "<b>使用示例：</b>\n"
        "• /unban\n"
        "• /unban 123456789\n\n"
        "<b>注意事项：</b>\n"
        "• 解除后用户可重新加入群组\n"
        "• 需要新的邀请链接或管理员添加\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "warn": (
        "⚠️ <b>/warn - 警告成员</b>\n\n"
        "<b>功能说明：</b>\n"
        "对用户发出警告，累计达到上限自动封禁。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复用户消息：/warn [原因]\n"
        "• 指定用户ID：/warn &lt;user_id&gt; [原因]\n"
        "• 选择用户：/warn @用户 [原因]（输入 @ 从列表选择）\n\n"
        "<b>使用示例：</b>\n"
        "• /warn 发送无关内容\n"
        "• /warn 123456789 违反群规\n\n"
        "<b>警告机制：</b>\n"
        "• 默认上限：3次警告\n"
        "• 达到上限：自动封禁\n"
        "• 查看记录：/warnings\n"
        "• 清除警告：/clearwarnings\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "warnings": (
        "📋 <b>/warnings - 查看警告记录</b>\n\n"
        "<b>功能说明：</b>\n"
        "查看用户的警告历史记录。\n\n"
        "<b>使用方法：</b>\n"
        "• 不指定用户：/warnings - 查看自己的警告\n"
        "• 回复用户消息：/warnings - 查看该用户的警告\n"
        "• 指定用户ID：/warnings &lt;user_id&gt;\n"
        "• 选择用户：/warnings @用户（输入 @ 从列表选择）\n\n"
        "<b>使用示例：</b>\n"
        "• /warnings - 查看自己的警告\n"
        "• /warnings 123456789 - 查看指定用户的警告\n\n"
        "<b>显示内容：</b>\n"
        "• 有效警告次数（30天内）\n"
        "• 警告原因和时间\n"
        "• 处罚阶梯提示\n"
        "• 过期警告标记\n\n"
        "<b>权限要求：</b>\n"
        "• 查看自己的警告 - 所有成员\n"
        "• 查看他人的警告 - 群组管理员"
    ),
    "clearwarnings": (
        "🗑️ <b>/clearwarnings - 清除警告</b>\n\n"
        "<b>功能说明：</b>\n"
        "清除用户的所有警告记录。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复用户消息：/clearwarnings\n"
        "• 指定用户ID：/clearwarnings &lt;user_id&gt;\n"
        "• 选择用户：/clearwarnings @用户（输入 @ 从列表选择）\n\n"
        "<b>使用示例：</b>\n"
        "• /clearwarnings\n"
        "• /clearwarnings 123456789\n\n"
        "<b>注意事项：</b>\n"
        "• 清除后无法恢复\n"
        "• 用户重新开始计数\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "delbefore": (
        "🗑️ <b>/delbefore - 删除往前的消息</b>\n\n"
        "<b>功能说明：</b>\n"
        "删除从指定消息往前（更早）的N条消息，包含被回复的消息本身。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复某条消息：/delbefore &lt;数量&gt;\n\n"
        "<b>使用示例：</b>\n"
        "• /delbefore 10 - 删除往前10条（包含被回复的消息）\n"
        "• /delbefore 50 - 删除往前50条（包含被回复的消息）\n\n"
        "<b>限制：</b>\n"
        "• 最多一次删除 1000 条\n"
        "• 必须回复某条消息\n"
        "• 只能删除 Bot 有权限删除的消息\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "delafter": (
        "🗑️ <b>/delafter - 删除往后的消息</b>\n\n"
        "<b>功能说明：</b>\n"
        "删除从指定消息往后（更晚）的N条消息，包含被回复的消息本身。\n\n"
        "<b>使用方法：</b>\n"
        "• 回复某条消息：/delafter &lt;数量&gt;\n\n"
        "<b>使用示例：</b>\n"
        "• /delafter 10 - 删除往后10条（包含被回复的消息）\n"
        "• /delafter 50 - 删除往后50条（包含被回复的消息）\n\n"
        "<b>限制：</b>\n"
        "• 最多一次删除 1000 条\n"
        "• 必须回复某条消息\n"
        "• 只能删除 Bot 有权限删除的消息\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "delrange": (
        "🗑️ <b>/delrange - 删除消息范围</b>\n\n"
        "<b>功能说明：</b>\n"
        "删除从起始消息到结束消息之间的所有消息。\n\n"
        "<b>使用方法：</b>\n"
        "1. 回复作为起始消息的某条消息\n"
        "2. 发送命令：/delrange &lt;结束消息ID或链接&gt;\n\n"
        "<b>使用示例：</b>\n"
        "• 回复某条消息后发送：/delrange 12345\n"
        "• 回复某条消息后发送：/delrange https://t.me/c/xxx/12345\n\n"
        "<b>提示：</b>\n"
        "• 电脑端右键消息可选择「复制消息链接」\n"
        "• 支持纯数字ID或完整消息链接\n"
        "• 起始和结束顺序可颠倒，自动识别\n\n"
        "<b>限制：</b>\n"
        "• 必须回复起始消息\n"
        "• 最多一次删除 1000 条\n"
        "• 只能删除 Bot 有权限删除的消息\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "spam": (
        "🚨 <b>/spam 或 /report - 举报垃圾消息</b>\n\n"
        "<b>功能说明：</b>\n"
        "举报某条消息为垃圾消息，用于训练反垃圾模型。\n"
        "（/report 和 /spam 功能完全相同，使用更方便记忆的命令即可）\n\n"
        "<b>使用方法：</b>\n"
        "• 普通成员：/spam [原因] - 提交举报\n"
        "• 管理员：/spam [原因] - 封禁用户并删除被回复的消息\n"
        "• 管理员：/spam -d [原因] - 封禁用户并删除该用户的所有消息\n\n"
        "<b>使用示例：</b>\n"
        "• /spam 发送广告\n"
        "• /spam -d 大量发送垃圾信息\n\n"
        "<b>举报流程（普通成员）：</b>\n"
        "1. 回复垃圾消息发送 /spam\n"
        "2. 管理员收到举报通知\n"
        "3. 管理员审核：/reports\n"
        "4. 接受处理：/approve &lt;举报ID&gt;\n\n"
        "<b>限制：</b>\n"
        "• 普通成员每天最多举报 10 次\n"
        "• 必须回复要举报的消息\n\n"
        "<b>训练作用：</b>\n"
        "• 标记的消息用于训练ML模型\n"
        "• 提高反垃圾检测准确率\n\n"
        "<b>权限要求：</b>所有成员"
    ),
    "notspam": (
        "✅ <b>/notspam、/nospam、/unspam - 标记非垃圾消息</b>\n\n"
        "<b>功能说明：</b>\n"
        "将误判的垃圾消息标记为正常消息，用于训练反垃圾模型。\n"
        "（/nospam 和 /unspam 是 /notspam 的别名，三者功能完全相同）\n\n"
        "<b>使用方法：</b>\n"
        "• 回复消息：/notspam [备注] - 预防性训练\n"
        "• 指定消息ID：/notspam &lt;消息ID或链接&gt; [备注] - 误报修正\n\n"
        "<b>支持的格式：</b>\n"
        "• 纯数字：12345\n"
        "• 私有群组链接：https://t.me/c/1234567890/12345\n"
        "• 公开群组链接：https://t.me/channel_name/12345\n\n"
        "<b>使用示例：</b>\n"
        "• /notspam （回复正常消息）\n"
        "• /notspam 这是正常讨论 （回复正常消息并添加备注）\n"
        "• /notspam 12345 （标记已删除的消息12345为误报）\n"
        "• /notspam https://t.me/c/1234567890/12345 （使用消息链接）\n\n"
        "<b>训练作用：</b>\n"
        "• 标记的消息用于训练ML模型\n"
        "• 增加负样本，减少误判率\n\n"
        "<b>限制：</b>\n"
        "• 仅管理员可用\n"
        "• 消息缓存有效期1小时\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "nospam": (
        "✅ <b>/nospam - 标记非垃圾消息</b>\n\n"
        "<b>说明：</b>\n"
        "/nospam 是 /notspam 命令的别名，两者功能完全相同。\n\n"
        "<b>查看详细用法：</b>\n"
        "请使用 /help notspam 查看完整的命令说明。\n\n"
        "<b>快速使用：</b>\n"
        "• 回复正常消息发送 /nospam [备注]\n"
        "• 将误判的消息标记为正常，用于训练模型\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "unspam": (
        "✅ <b>/unspam - 标记非垃圾消息</b>\n\n"
        "<b>说明：</b>\n"
        "/unspam 是 /notspam 命令的别名，两者功能完全相同。\n\n"
        "<b>查看详细用法：</b>\n"
        "请使用 /help notspam 查看完整的命令说明。\n\n"
        "<b>快速使用：</b>\n"
        "• 回复正常消息发送 /unspam [备注]\n"
        "• 将误判的消息标记为正常，用于训练模型\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "reports": (
        "📋 <b>/reports - 查看举报列表</b>\n\n"
        "<b>功能说明：</b>\n"
        "查看群组内待处理的举报列表。\n\n"
        "<b>使用方法：</b>\n"
        "• 在群组中发送：/reports\n\n"
        "<b>显示内容：</b>\n"
        "• 举报ID\n"
        "• 被举报消息内容\n"
        "• 举报人\n"
        "• 举报时间\n"
        "• 处理按钮\n\n"
        "<b>后续操作：</b>\n"
        "• 点击按钮快速处理\n"
        "• 或使用 /approve 命令接受\n"
        "• 或使用 /reject 命令拒绝\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "approve": (
        "✅ <b>/approve - 接受举报</b>\n\n"
        "<b>功能说明：</b>\n"
        "接受垃圾消息举报，封禁用户并删除消息。\n\n"
        "<b>使用方法：</b>\n"
        "• /approve &lt;举报ID&gt;\n\n"
        "<b>使用示例：</b>\n"
        "• /approve 123\n"
        "• /approve 456\n\n"
        "<b>处理结果：</b>\n"
        "• 封禁被举报用户\n"
        "• 删除被举报的消息\n"
        "• 添加到垃圾样本训练库\n"
        "• 标记举报状态为已接受\n\n"
        "<b>提示：</b>\n"
        "• 使用 /reports 查看待处理举报\n"
        "• 举报ID在举报列表中显示\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "reject": (
        "❌ <b>/reject - 拒绝举报</b>\n\n"
        "<b>功能说明：</b>\n"
        "拒绝垃圾消息举报，将举报标记为误报或不需要处理。\n\n"
        "<b>使用方法：</b>\n"
        "• /reject &lt;举报ID&gt;\n\n"
        "<b>使用示例：</b>\n"
        "• /reject 123\n"
        "• /reject 456\n\n"
        "<b>处理结果：</b>\n"
        "• 不封禁用户\n"
        "• 不删除消息\n"
        "• 标记举报状态为已拒绝\n\n"
        "<b>使用场景：</b>\n"
        "• 举报内容不属于垃圾消息\n"
        "• 误报或恶意举报\n"
        "• 不需要处理的正常消息\n\n"
        "<b>提示：</b>\n"
        "• 使用 /reports 查看待处理举报\n"
        "• 举报ID在举报列表中显示\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "report": (
        "🚨 <b>/report - 举报垃圾消息</b>\n\n"
        "<b>说明：</b>\n"
        "/report 是 /spam 命令的别名，两者功能完全相同。\n\n"
        "<b>查看详细用法：</b>\n"
        "请使用 /help spam 查看完整的命令说明。\n\n"
        "<b>快速使用：</b>\n"
        "• 回复垃圾消息发送 /report [原因]\n"
        "• 普通成员：提交举报给管理员\n"
        "• 管理员：直接封禁用户并删除消息\n\n"
        "<b>权限要求：</b>所有成员"
    ),
    "cleanup": (
        "🧹 <b>/cleanup - 清理不活跃用户</b>\n\n"
        "<b>功能说明：</b>\n"
        "清理群组中的已删除用户和长期不活跃的用户。\n\n"
        "<b>使用方法：</b>\n"
        "• /cleanup - 预览清理（显示待清理用户数量）\n"
        "• /cleanup run - 执行清理（已删除 + 很久不上线）\n"
        "• /cleanup deleted - 仅清理已删除用户\n"
        "• /cleanup inactive - 仅清理很久不上线的用户（安全模式）\n"
        "• /cleanup refresh - 强制刷新成员缓存\n"
        "• /cleanup cache - 查看缓存状态\n\n"
        "<b>安全设计：</b>\n"
        "• inactive 子命令只清理「很久不上线」的用户\n"
        "• 避免误删暂时不活跃但仍正常的群组成员\n"
        "• 只清理确实应该被清理的用户（已删除账号 + 长期离线）\n\n"
        "<b>缓存机制：</b>\n"
        "• 成员列表会缓存 1 小时，减少 API 调用\n"
        "• 使用 /cleanup refresh 可强制刷新缓存\n"
        "• 使用 /cleanup cache 查看缓存状态\n\n"
        "<b>注意事项：</b>\n"
        "• 需要 Telethon 客户端支持\n"
        "• 管理员不会被清理\n"
        "• 清理操作不可撤销\n"
        "• 被踢出的用户可重新加入\n\n"
        "<b>权限要求：</b>群组管理员"
    ),
    "whitelist": (
        "📋 <b>/whitelist - 白名单管理</b>\n\n"
        "<b>功能说明：</b>\n"
        "管理 Bot 的群组白名单，只有白名单内的群组才能使用 Bot。\n\n"
        "<b>使用方法：</b>\n"
        "• 列出白名单：/whitelist\n"
        "• 添加群组：/whitelist add &lt;chat_id&gt; [群组名称]\n"
        "• 移除群组：/whitelist remove &lt;chat_id&gt;\n\n"
        "<b>使用示例：</b>\n"
        "• /whitelist - 查看所有白名单群组\n"
        "• /whitelist add -1001234567890 测试群组\n"
        "• /whitelist remove -1001234567890\n\n"
        "<b>注意事项：</b>\n"
        "• chat_id 必须是数字格式\n"
        "• 群组名称为可选参数\n"
        "• 移除后 Bot 将自动退出该群组\n\n"
        "<b>权限要求：</b>超级管理员"
    ),
    "help": (
        "❓ <b>/help - 帮助命令</b>\n\n"
        "<b>功能说明：</b>\n"
        "查看命令列表或某个命令的详细用法。\n\n"
        "<b>使用方法：</b>\n"
        "• /help - 显示命令列表\n"
        "• /help &lt;命令&gt; - 查看命令详细说明\n\n"
        "<b>使用示例：</b>\n"
        "• /help\n"
        "• /help groupset\n"
        "• /help kick\n"
        "• /help warn\n\n"
        "<b>提示：</b>\n"
        "• 输入 / 可自动显示可用命令\n"
        "• 群组管理员和普通成员看到的命令不同\n\n"
        "<b>权限要求：</b>所有用户"
    ),
}


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """帮助命令 - 支持查看具体命令的详细用法"""
    # 类型检查
    if not message.text:
        return

    # 解析参数
    args = message.text.split(maxsplit=1)

    # 如果没有参数，显示通用帮助
    if len(args) == 1:
        await cmd_start(message)
        return

    # 获取命令名称（去掉可能的 / 前缀）
    command = args[1].lstrip("/").lower()

    # 查找命令帮助
    if command in COMMAND_HELP:
        await message.answer(COMMAND_HELP[command])
    else:
        await message.answer(
            f"❌ 未找到命令 <code>/{command}</code> 的帮助信息\n\n" f"使用 /help 查看所有可用命令",
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
                f"<b>说明：</b>\n"
                f"• 启用后，新用户需通过发送文本消息积累活跃度\n"
                f"• 活跃度 > 0 才能发送图片、贴纸、转发等非文本消息\n"
                f"• 发送文本消息 +1，发送非文本消息 -2\n"
                f"• 每日无消息自动衰减 -1",
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
        if not message.text:
            return

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


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    """健康检查命令（仅超级管理员）"""
    # 检查是否是超级管理员
    if not message.from_user:
        return

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
    if not message.from_user:
        return

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
            await GroupRepository.update_activity_settings(chat_id, True)
            logger.info(f"管理员 {callback.from_user.id} 在群组 {chat_id} 启用了活跃度系统")
            await callback.answer("✅ 活跃度系统已启用", show_alert=True)

        elif action == "disable":
            if not group.activity_enabled:
                await callback.answer("ℹ️ 活跃度系统已经是禁用状态", show_alert=True)
                return

            await GroupRepository.update_activity_settings(chat_id, False)
            await GroupRepository.update_activity_settings(chat_id, False)
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
