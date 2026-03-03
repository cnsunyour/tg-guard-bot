"""上下文服务 - 群组消息上下文和回复链管理"""

import json
import time
from typing import TypedDict, cast

from aiogram.types import Message
from loguru import logger

from src.core.config import settings
from src.core.redis import RedisKeys, get_redis


class ContextMessage(TypedDict):
    """上下文消息类型"""

    user_id: int
    user_name: str
    text: str
    timestamp: int
    message_id: int


class ConversationContext(TypedDict):
    """对话上下文类型"""

    reply_chain: list[ContextMessage]  # 回复链（从被回复的消息到当前消息）
    recent_messages: list[ContextMessage]  # 群组最近消息


class ContextService:
    """上下文服务 - 管理群组上下文和回复链"""

    @staticmethod
    async def record_message(message: Message) -> None:
        """记录消息到群组上下文缓存

        使用 Redis List 存储，LPUSH 新消息，LTRIM 保持固定长度

        Args:
            message: Telegram 消息对象
        """
        if not settings.context_enabled:
            return

        # 只记录文本消息
        if not message.text or not message.from_user:
            return

        # 跳过私聊
        if message.chat.type == "private":
            return

        try:
            redis = get_redis()
            context_key = RedisKeys.group_context(message.chat.id)

            # 构建消息数据
            context_msg: ContextMessage = {
                "user_id": message.from_user.id,
                "user_name": message.from_user.full_name or "Unknown",
                "text": message.text[: settings.context_max_text_length],  # 截断过长文本
                "timestamp": int(time.time()),
                "message_id": message.message_id,
            }

            # ✅ 使用 Redis pipeline 减少 RTT
            async with redis.pipeline(transaction=True) as pipe:
                # LPUSH 新消息到列表头部
                await pipe.lpush(context_key, json.dumps(context_msg, ensure_ascii=False))
                # LTRIM 保持最多 N 条消息
                await pipe.ltrim(context_key, 0, settings.context_message_count - 1)
                # 设置 TTL
                ttl_seconds = settings.context_ttl_minutes * 60
                await pipe.expire(context_key, ttl_seconds)
                # 执行 pipeline
                await pipe.execute()

            logger.debug(
                f"已记录上下文消息 [群组:{message.chat.id}] "
                f"[用户:{message.from_user.id}] [消息ID:{message.message_id}]"
            )

        except Exception as e:
            logger.error(f"记录上下文消息失败: {e}")

    @staticmethod
    async def get_group_context(chat_id: int, limit: int | None = None) -> list[ContextMessage]:
        """获取群组最近消息上下文

        Args:
            chat_id: 群组 ID
            limit: 限制返回数量（None 则使用配置值）

        Returns:
            上下文消息列表（按时间倒序）
        """
        if not settings.context_enabled:
            return []

        try:
            redis = get_redis()
            context_key = RedisKeys.group_context(chat_id)

            # 获取最近消息（LRANGE 0 到 limit-1）
            if limit is None:
                limit = settings.context_message_count

            messages_json = await redis.lrange(context_key, 0, limit - 1)

            # 解析 JSON
            messages: list[ContextMessage] = []
            for msg_json in messages_json:
                try:
                    msg = json.loads(msg_json)
                    messages.append(msg)
                except json.JSONDecodeError as e:
                    logger.warning(f"解析上下文消息失败: {e}")
                    continue

            logger.debug(f"获取群组上下文 [群组:{chat_id}] [消息数:{len(messages)}]")
            return messages

        except Exception as e:
            logger.error(f"获取群组上下文失败: {e}")
            return []

    @staticmethod
    async def build_reply_chain(
        message: Message, max_depth: int | None = None
    ) -> list[ContextMessage]:
        """递归构建回复链

        从当前消息开始，向上追溯 reply_to_message，直到没有更多回复或达到最大深度

        Args:
            message: 当前消息
            max_depth: 最大追溯深度（None 则使用配置值）

        Returns:
            回复链列表（从被回复的消息到当前消息，按时间顺序）
        """
        if not settings.context_enabled:
            return []

        if max_depth is None:
            max_depth = settings.context_reply_depth

        try:
            chain: list[ContextMessage] = []
            current = message
            depth = 0

            # 递归向上追溯
            while current.reply_to_message and depth < max_depth:
                replied_msg = current.reply_to_message

                # 只处理文本消息
                if replied_msg.text and replied_msg.from_user:
                    context_msg: ContextMessage = {
                        "user_id": replied_msg.from_user.id,
                        "user_name": replied_msg.from_user.full_name or "Unknown",
                        "text": replied_msg.text[: settings.context_max_text_length],
                        "timestamp": int(replied_msg.date.timestamp()) if replied_msg.date else 0,
                        "message_id": replied_msg.message_id,
                    }
                    # 插入到链头（保持时间顺序）
                    chain.insert(0, context_msg)

                current = replied_msg
                depth += 1

            logger.debug(f"构建回复链 [深度:{len(chain)}] [消息ID:{message.message_id}]")
            return chain

        except Exception as e:
            logger.error(f"构建回复链失败: {e}")
            return []

    @staticmethod
    async def get_conversation_context(message: Message) -> ConversationContext:
        """获取完整对话上下文（回复链 + 群组最近消息）

        Args:
            message: 当前消息

        Returns:
            对话上下文字典
        """
        if not settings.context_enabled:
            return {"reply_chain": [], "recent_messages": []}

        try:
            # 并行获取回复链和群组上下文
            import asyncio

            results = await asyncio.gather(
                ContextService.build_reply_chain(message),
                ContextService.get_group_context(message.chat.id),
                return_exceptions=True,
            )

            # 处理异常
            final_reply_chain: list[ContextMessage] = []
            final_recent_messages: list[ContextMessage] = []

            # Type guard for first result
            if not isinstance(results[0], Exception):
                final_reply_chain = cast("list[ContextMessage]", results[0])
            else:
                logger.error(f"获取回复链失败: {results[0]}")

            # Type guard for second result
            if not isinstance(results[1], Exception):
                final_recent_messages = cast("list[ContextMessage]", results[1])
            else:
                logger.error(f"获取群组上下文失败: {results[1]}")

            return ConversationContext(
                reply_chain=final_reply_chain,
                recent_messages=final_recent_messages,
            )

        except Exception as e:
            logger.error(f"获取对话上下文失败: {e}")
            return {"reply_chain": [], "recent_messages": []}

    @staticmethod
    def format_context_for_ai(
        context: ConversationContext,
        current_text: str,
        current_message_id: int | None = None,
        chat_title: str | None = None,
        chat_description: str | None = None,
    ) -> str:
        """格式化上下文供 AI 使用

        Args:
            context: 对话上下文
            current_text: 当前待检测消息文本
            current_message_id: 当前消息 ID（用于排除自身）
            chat_title: 群组名称
            chat_description: 群组简介

        Returns:
            格式化的上下文字符串
        """
        parts = []

        # 群组信息（帮助 AI 理解群组话题，降低误判）
        if chat_title:
            group_info = f"【群组信息】\n群名称：{chat_title}"
            if chat_description:
                group_info += f"\n群简介：{chat_description}"
            parts.append(group_info)

        # 回复链
        if context["reply_chain"]:
            parts.append("【对话回复链】")
            for msg in context["reply_chain"]:
                parts.append(f"{msg['user_name']}: {msg['text']}")

        # 群组最近对话（排除当前消息，按时间正序）
        if context["recent_messages"]:
            parts.append("\n【群组最近对话】")
            # 过滤当前消息 + 倒序（最老的在前）
            filtered_messages = [
                msg
                for msg in reversed(context["recent_messages"])
                if current_message_id is None or msg["message_id"] != current_message_id
            ]
            for msg in filtered_messages[:5]:  # 只取最近 5 条
                parts.append(f"{msg['user_name']}: {msg['text']}")

        # 当前消息
        parts.append("\n【待检测消息】")
        parts.append(current_text)

        return "\n".join(parts)
