"""验证服务模块"""

import secrets
from typing import Optional
from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from src.core.redis import get_redis, RedisKeys
from src.core.config import settings
from src.core.utils import escape_html


@dataclass
class VerificationChallenge:
    """验证挑战数据"""

    challenge_type: str  # button, math, slider
    question: str
    answer: str
    keyboard: InlineKeyboardMarkup


class VerificationService:
    """入群验证服务"""

    @staticmethod
    async def generate_button_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成按钮验证挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)，✅ P1-7: 使用群组配置
        """
        # 生成简单的确认按钮
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ 我是人类", callback_data=f"verify_btn:{chat_id}:{user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ 取消", callback_data=f"verify_cancel:{chat_id}:{user_id}"
                    )
                ],
            ]
        )

        # ✅ P1-7: 使用传入的超时参数而非全局配置
        question = f"👋 欢迎 {escape_html(username)}！\n\n请在 {timeout} 秒内点击按钮完成验证"
        answer = "button_click"

        # 存储验证状态到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,  # ✅ P1-7: 使用传入的超时参数
            "button",  # 验证类型
        )

        return VerificationChallenge(
            challenge_type="button", question=question, answer=answer, keyboard=keyboard
        )

    @staticmethod
    async def generate_math_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成数学验证码挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)，✅ P1-7: 使用群组配置
        """
        # 生成简单的加法题（使用密码学安全的随机数）
        num1 = secrets.randbelow(10) + 1  # 1-10
        num2 = secrets.randbelow(10) + 1  # 1-10
        correct_answer = num1 + num2

        # 生成4个选项，包含正确答案
        options = [correct_answer]
        while len(options) < 4:
            wrong = secrets.randbelow(20) + 1  # 1-20
            if wrong not in options:
                options.append(wrong)

        # 使用密码学安全的随机打乱
        options = list(options)  # 确保是列表
        for i in range(len(options) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            options[i], options[j] = options[j], options[i]

        # 创建按钮
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=str(options[0]),
                        callback_data=f"verify_math:{chat_id}:{user_id}:{options[0]}",
                    ),
                    InlineKeyboardButton(
                        text=str(options[1]),
                        callback_data=f"verify_math:{chat_id}:{user_id}:{options[1]}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=str(options[2]),
                        callback_data=f"verify_math:{chat_id}:{user_id}:{options[2]}",
                    ),
                    InlineKeyboardButton(
                        text=str(options[3]),
                        callback_data=f"verify_math:{chat_id}:{user_id}:{options[3]}",
                    ),
                ],
            ]
        )

        question = (
            f"👋 欢迎 {escape_html(username)}！\n\n"
            f"请在 {timeout} 秒内回答问题：\n\n"  # ✅ P1-7: 使用传入的超时参数
            f"❓ {num1} + {num2} = ?"
        )

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,  # ✅ P1-7: 使用传入的超时参数
            f"math:{correct_answer}",  # 格式: math:答案
        )

        return VerificationChallenge(
            challenge_type="math",
            question=question,
            answer=str(correct_answer),
            keyboard=keyboard,
        )

    @staticmethod
    async def generate_slider_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成滑块验证挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)，✅ P1-7: 使用群组配置
        """
        # 生成4个位置，只有一个是正确的（使用密码学安全的随机数）
        correct_position = secrets.randbelow(4)  # 0-3
        emojis = ["⬜", "⬜", "⬜", "⬜"]
        emojis[correct_position] = "🟩"

        # 创建按钮
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=emojis[0],
                        callback_data=f"verify_slider:{chat_id}:{user_id}:0",
                    ),
                    InlineKeyboardButton(
                        text=emojis[1],
                        callback_data=f"verify_slider:{chat_id}:{user_id}:1",
                    ),
                    InlineKeyboardButton(
                        text=emojis[2],
                        callback_data=f"verify_slider:{chat_id}:{user_id}:2",
                    ),
                    InlineKeyboardButton(
                        text=emojis[3],
                        callback_data=f"verify_slider:{chat_id}:{user_id}:3",
                    ),
                ],
            ]
        )

        question = (
            f"👋 欢迎 {escape_html(username)}！\n\n"
            f"请在 {timeout} 秒内点击绿色方块：\n\n"  # ✅ P1-7: 使用传入的超时参数
            f"{''.join(emojis)}"
        )

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,  # ✅ P1-7: 使用传入的超时参数
            f"slider:{correct_position}",  # 格式: slider:位置
        )

        return VerificationChallenge(
            challenge_type="slider",
            question=question,
            answer=str(correct_position),
            keyboard=keyboard,
        )

    @staticmethod
    async def verify_answer(chat_id: int, user_id: int, answer: str) -> bool:
        """验证答案是否正确"""
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)

        stored_value = await redis.get(key)
        if not stored_value:
            return False  # 验证已过期

        # 解析存储的值
        if ":" in stored_value:
            challenge_type, correct_answer = stored_value.split(":", 1)
            return answer == correct_answer
        else:
            # 按钮验证类型
            return stored_value == "button"

    @staticmethod
    async def clear_verification(chat_id: int, user_id: int) -> None:
        """清除验证状态"""
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.delete(key)

    @staticmethod
    async def is_verification_pending(chat_id: int, user_id: int) -> bool:
        """检查是否有待验证状态"""
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        return await redis.exists(key) > 0
