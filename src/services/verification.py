"""验证服务模块"""

import io
import secrets
from dataclasses import dataclass

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from captcha.image import ImageCaptcha

from src.core.redis import RedisKeys, get_redis
from src.core.utils import escape_html
from src.data.verification.emoji_mapping import EMOJI_MAPPINGS
from src.data.verification.qa_questions import QA_QUESTIONS


@dataclass
class VerificationChallenge:
    """验证挑战数据"""

    challenge_type: str  # math, slider, qa, emoji, captcha, honeypot, random
    question: str
    answer: str
    keyboard: InlineKeyboardMarkup
    photo: BufferedInputFile | None = None  # 用于 captcha 验证


class VerificationService:
    """入群验证服务"""

    @staticmethod
    async def generate_math_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成数学验证码挑战 - 支持四则运算，最多两步

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)
        """
        # 50% 概率生成 1 步或 2 步运算
        is_two_step = secrets.randbelow(2) == 1

        if is_two_step:
            # 两步运算：(a op1 b) op2 c
            expression, correct_answer = VerificationService._generate_two_step_math()
        else:
            # 一步运算：a op b
            expression, correct_answer = VerificationService._generate_one_step_math()

        # 生成4个选项，包含正确答案
        options = [correct_answer]
        while len(options) < 4:
            # 在正确答案附近生成错误答案，增加难度
            if correct_answer <= 10:
                wrong = secrets.randbelow(20) + 1
            elif correct_answer <= 50:
                offset = secrets.randbelow(20) - 10  # -10 到 +9
                wrong = max(1, min(100, correct_answer + offset))
            else:
                offset = secrets.randbelow(40) - 20  # -20 到 +19
                wrong = max(1, min(100, correct_answer + offset))

            if wrong not in options:
                options.append(wrong)

        # 使用密码学安全的随机打乱（Fisher-Yates）
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
            f"请在 {timeout} 秒内回答问题：\n\n"
            f"❓ {expression} = ?"
        )

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,
            f"math:{correct_answer}",
        )

        return VerificationChallenge(
            challenge_type="math",
            question=question,
            answer=str(correct_answer),
            keyboard=keyboard,
        )

    @staticmethod
    def _generate_one_step_math() -> tuple[str, int]:
        """生成一步运算题目

        Returns:
            (expression, answer): 例如 ("3 + 5", 8)
        """
        op = secrets.choice(["+", "-", "×", "÷"])

        if op == "+":
            # 加法：结果不超过 100
            a = secrets.randbelow(99) + 1  # 1-99
            b = secrets.randbelow(100 - a) + 1  # 确保 a + b <= 100
            return f"{a} + {b}", a + b

        elif op == "-":
            # 减法：确保结果非负
            a = secrets.randbelow(100) + 1  # 1-100
            b = secrets.randbelow(a) + 1  # 1 <= b <= a，确保 a - b >= 0
            return f"{a} - {b}", a - b

        elif op == "×":
            # 乘法：结果不超过 100
            a = secrets.randbelow(10) + 1  # 1-10
            b = secrets.randbelow(100 // a) + 1  # 确保 a * b <= 100
            return f"{a} × {b}", a * b

        else:  # "÷"
            # 除法：确保能整除，结果在 1-100
            b = secrets.randbelow(10) + 1  # 除数 1-10
            quotient = secrets.randbelow(min(100 // b, 100)) + 1  # 商 1-100
            a = b * quotient  # 被除数
            return f"{a} ÷ {b}", quotient

    @staticmethod
    def _generate_two_step_math() -> tuple[str, int]:
        """生成两步运算题目

        Returns:
            (expression, answer): 例如 ("(3 + 5) × 2", 16)
        """
        # 随机选择运算符
        op1 = secrets.choice(["+", "-", "×", "÷"])
        op2 = secrets.choice(["+", "-", "×", "÷"])

        # 生成第一步运算，确保中间结果在 1-50 范围内（为第二步留空间）
        max_tries = 100
        for _ in range(max_tries):
            if op1 == "+":
                a = secrets.randbelow(25) + 1
                b = secrets.randbelow(25) + 1
                intermediate = a + b
            elif op1 == "-":
                a = secrets.randbelow(50) + 1
                b = secrets.randbelow(a) + 1
                intermediate = a - b
            elif op1 == "×":
                a = secrets.randbelow(7) + 1
                b = secrets.randbelow(7) + 1
                intermediate = a * b
            else:  # "÷"
                b = secrets.randbelow(7) + 1
                intermediate = secrets.randbelow(7) + 1
                a = b * intermediate

            # 确保中间结果合理（1-50）
            if not (1 <= intermediate <= 50):
                continue

            # 生成第二步运算
            if op2 == "+":
                c = secrets.randbelow(100 - intermediate) + 1
                final = intermediate + c
            elif op2 == "-":
                c = secrets.randbelow(intermediate) + 1
                final = intermediate - c
            elif op2 == "×":
                c = secrets.randbelow(min(100 // intermediate, 10)) + 1
                final = intermediate * c
            else:  # "÷"
                # 确保能整除
                if intermediate == 1:
                    c = 1
                    final = 1
                else:
                    # 找 intermediate 的因子
                    divisors = [i for i in range(1, intermediate + 1) if intermediate % i == 0]
                    c = secrets.choice(divisors)
                    final = intermediate // c

            # 确保最终结果在 1-100 范围内
            if 1 <= final <= 100:
                return f"({a} {op1} {b}) {op2} {c}", final

        # 如果多次尝试失败，回退到简单的加法
        a, b, c = secrets.randbelow(30) + 1, secrets.randbelow(30) + 1, secrets.randbelow(30) + 1
        return f"({a} + {b}) + {c}", a + b + c

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
    async def generate_qa_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成问答验证挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)
        """
        # 从题库随机选择一题
        qa = secrets.choice(QA_QUESTIONS)
        question_text = qa["question"]
        options = qa["options"]
        correct_index = qa["answer"]

        # 创建按钮（2行2列）
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=options[0],
                        callback_data=f"verify_qa:{chat_id}:{user_id}:0",
                    ),
                    InlineKeyboardButton(
                        text=options[1],
                        callback_data=f"verify_qa:{chat_id}:{user_id}:1",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=options[2],
                        callback_data=f"verify_qa:{chat_id}:{user_id}:2",
                    ),
                    InlineKeyboardButton(
                        text=options[3],
                        callback_data=f"verify_qa:{chat_id}:{user_id}:3",
                    ),
                ],
            ]
        )

        question = (
            f"👋 欢迎 {escape_html(username)}！\n\n"
            f"请在 {timeout} 秒内回答问题：\n\n"
            f"❓ {question_text}"
        )

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,
            f"qa:{correct_index}",
        )

        return VerificationChallenge(
            challenge_type="qa",
            question=question,
            answer=str(correct_index),
            keyboard=keyboard,
        )

    @staticmethod
    async def generate_emoji_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成表情验证挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)
        """
        # 从映射表随机选择一个
        mapping = secrets.choice(EMOJI_MAPPINGS)
        description = mapping["description"]
        correct_emoji = mapping["correct"]
        decoys = mapping["decoys"]

        # 组合所有选项并打乱
        all_emojis = [correct_emoji, *decoys]
        for i in range(len(all_emojis) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            all_emojis[i], all_emojis[j] = all_emojis[j], all_emojis[i]

        # 找到正确答案的新位置
        correct_index = all_emojis.index(correct_emoji)

        # 创建按钮
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=all_emojis[0],
                        callback_data=f"verify_emoji:{chat_id}:{user_id}:0",
                    ),
                    InlineKeyboardButton(
                        text=all_emojis[1],
                        callback_data=f"verify_emoji:{chat_id}:{user_id}:1",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=all_emojis[2],
                        callback_data=f"verify_emoji:{chat_id}:{user_id}:2",
                    ),
                    InlineKeyboardButton(
                        text=all_emojis[3],
                        callback_data=f"verify_emoji:{chat_id}:{user_id}:3",
                    ),
                ],
            ]
        )

        question = (
            f"👋 欢迎 {escape_html(username)}！\n\n"
            f"请在 {timeout} 秒内选择对应的表情：\n\n"
            f"❓ {description}"
        )

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,
            f"emoji:{correct_index}",
        )

        return VerificationChallenge(
            challenge_type="emoji",
            question=question,
            answer=str(correct_index),
            keyboard=keyboard,
        )

    @staticmethod
    async def generate_captcha_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成图片验证码挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)
        """
        # 生成随机验证码（4位数字+字母）
        chars = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # 排除易混淆字符 I,O
        captcha_text = "".join(secrets.choice(chars) for _ in range(4))

        # 生成验证码图片
        image_captcha = ImageCaptcha(width=200, height=80)
        image_data = image_captcha.generate(captcha_text)

        # 转换为 BufferedInputFile
        image_bytes = io.BytesIO()
        image_bytes.write(image_data.getvalue())
        image_bytes.seek(0)
        photo = BufferedInputFile(image_bytes.read(), filename="captcha.png")

        # 创建按钮
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ 输入验证码",
                        callback_data=f"verify_captcha_input:{chat_id}:{user_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 换一张",
                        callback_data=f"verify_captcha_refresh:{chat_id}:{user_id}",
                    ),
                ],
            ]
        )

        question = (
            f"👋 欢迎 {escape_html(username)}！\n\n"
            f"请在 {timeout} 秒内输入图片中的验证码（不区分大小写）："
        )

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,
            f"captcha:{captcha_text.upper()}",
        )

        return VerificationChallenge(
            challenge_type="captcha",
            question=question,
            answer=captcha_text.upper(),
            keyboard=keyboard,
            photo=photo,
        )

    @staticmethod
    async def generate_honeypot_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成蜜罐验证挑战

        蜜罐验证包含一个数学题和诱饵按钮，机器人可能会点击诱饵

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)
        """
        # 生成简单的加法题
        num1 = secrets.randbelow(10) + 1
        num2 = secrets.randbelow(10) + 1
        correct_answer = num1 + num2

        # 生成诱饵按钮文本（看起来像"跳过"或"通过"）
        decoy_texts = ["✅ 跳过验证", "✅ 直接通过", "✅ 我是人类"]
        decoy_text = secrets.choice(decoy_texts)

        # 生成错误答案
        wrong_answers = []
        while len(wrong_answers) < 2:
            wrong = secrets.randbelow(20) + 1
            if wrong != correct_answer and wrong not in wrong_answers:
                wrong_answers.append(wrong)

        # 创建按钮布局：第一行是诱饵，第二行是真实选项
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=decoy_text,
                        callback_data=f"verify_honeypot:{chat_id}:{user_id}:trap",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=str(wrong_answers[0]),
                        callback_data=f"verify_honeypot:{chat_id}:{user_id}:{wrong_answers[0]}",
                    ),
                    InlineKeyboardButton(
                        text=str(correct_answer),
                        callback_data=f"verify_honeypot:{chat_id}:{user_id}:{correct_answer}",
                    ),
                    InlineKeyboardButton(
                        text=str(wrong_answers[1]),
                        callback_data=f"verify_honeypot:{chat_id}:{user_id}:{wrong_answers[1]}",
                    ),
                ],
            ]
        )

        question = (
            f"👋 欢迎 {escape_html(username)}！\n\n"
            f"请在 {timeout} 秒内回答问题：\n\n"
            f"❓ {num1} + {num2} = ?"
        )

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout,
            f"honeypot:{correct_answer}",
        )

        return VerificationChallenge(
            challenge_type="honeypot",
            question=question,
            answer=str(correct_answer),
            keyboard=keyboard,
        )

    @staticmethod
    async def generate_random_challenge(
        chat_id: int, user_id: int, username: str, timeout: int = 60
    ) -> VerificationChallenge:
        """生成随机类型的验证挑战

        从可用的验证类型中随机选择一种

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            username: 用户名
            timeout: 验证超时时间(秒)
        """
        # 可用的验证类型（不包括 random 自身）
        available_types = ["math", "slider", "qa", "emoji", "captcha", "honeypot"]
        selected_type = secrets.choice(available_types)

        # 根据选择的类型生成对应的挑战
        if selected_type == "math":
            return await VerificationService.generate_math_challenge(
                chat_id, user_id, username, timeout
            )
        elif selected_type == "slider":
            return await VerificationService.generate_slider_challenge(
                chat_id, user_id, username, timeout
            )
        elif selected_type == "qa":
            return await VerificationService.generate_qa_challenge(
                chat_id, user_id, username, timeout
            )
        elif selected_type == "emoji":
            return await VerificationService.generate_emoji_challenge(
                chat_id, user_id, username, timeout
            )
        elif selected_type == "captcha":
            return await VerificationService.generate_captcha_challenge(
                chat_id, user_id, username, timeout
            )
        else:  # honeypot
            return await VerificationService.generate_honeypot_challenge(
                chat_id, user_id, username, timeout
            )

    @staticmethod
    async def verify_answer(chat_id: int, user_id: int, answer: str) -> bool:
        """验证答案是否正确

        特殊情况:
        - honeypot: answer == "trap" 表示点击了诱饵，返回 False
        - captcha: 忽略大小写比较
        """
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)

        stored_value = await redis.get(key)
        if not stored_value:
            return False  # 验证已过期

        # 蜜罐陷阱检测
        if answer == "trap":
            return False

        # 解析存储的值
        if ":" in stored_value:
            challenge_type, correct_answer = stored_value.split(":", 1)

            # captcha 验证忽略大小写
            if challenge_type == "captcha":
                return answer.upper() == correct_answer.upper()

            # 其他类型精确匹配
            return answer == correct_answer

        # 不应该到达这里（button 验证已删除）
        return False

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
