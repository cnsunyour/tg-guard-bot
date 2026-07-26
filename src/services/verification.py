"""验证服务模块

领域模型采用 discriminated union：每个 generate_* 只返回该类型挑战的纯业务数据
（题面、选项、答案载体），不接收 username、不构建带文案的 Telegram keyboard、不拼装
展示文本。展示层（文案 + keyboard）由 verification_render 按 locale 渲染，从而支持 i18n。
所有可变集合字段用 tuple，配合 frozen=True 保证不可变。
"""

from __future__ import annotations

import io
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from aiogram.types import BufferedInputFile
from captcha.image import ImageCaptcha

from src.core.redis import RedisKeys, get_redis
from src.data.verification.emoji_mapping import EMOJI_MAPPINGS
from src.data.verification.qa_questions import QA_QUESTIONS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# WebApp 验证的 5 个 provider 共用 WebAppChallenge，用 provider 字段区分
type WebAppProvider = Literal["turnstile", "friendly", "hcaptcha", "mtcaptcha", "altcha"]
# 蜜罐诱饵用稳定 code（非中文文案），renderer 按 locale 映射展示文本
type HoneypotDecoy = Literal["skip", "direct", "human"]
_HONEYPOT_DECOYS: tuple[HoneypotDecoy, ...] = ("skip", "direct", "human")


@dataclass(frozen=True, slots=True)
class MathChallenge:
    """数学验证：结构化表达式 + 4 个选项（顺序即按钮顺序）"""

    expression: str
    choices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SliderChallenge:
    """滑块验证：4 个方格，绿色方格为正确位置"""

    cells: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QAChallenge:
    """问答验证：题面 + 选项文案走 catalog（verification.qa.bank.<id>.*）"""

    question_id: str


@dataclass(frozen=True, slots=True)
class EmojiChallenge:
    """表情验证：描述文案走 catalog（verification.emoji.bank.<id>.description）"""

    description_id: str
    emojis: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    """图片验证码：仅携带生成的图片，文案/按钮由 renderer 渲染"""

    photo: BufferedInputFile


@dataclass(frozen=True, slots=True)
class HoneypotChallenge:
    """蜜罐验证：表达式 + 3 个真实选项 + 诱饵 code"""

    expression: str
    choices: tuple[int, ...]
    decoy: HoneypotDecoy


@dataclass(frozen=True, slots=True)
class PuzzleChallenge:
    """拼图验证：仅携带生成的图片，选项为固定 4 个位置"""

    photo: BufferedInputFile


@dataclass(frozen=True, slots=True)
class WebAppChallenge:
    """WebApp 验证（turnstile/friendly/hcaptcha/mtcaptcha/altcha）"""

    provider: WebAppProvider
    webapp_url: str


type VerificationChallenge = (
    MathChallenge
    | SliderChallenge
    | QAChallenge
    | EmojiChallenge
    | CaptchaChallenge
    | HoneypotChallenge
    | PuzzleChallenge
    | WebAppChallenge
)


class VerificationService:
    """入群验证服务"""

    @staticmethod
    async def generate_math_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> MathChallenge:
        """生成数学验证码挑战 - 支持四则运算，最多两步

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
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

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout + 10,  # ✅ TTL 比超时时间多 10 秒，避免竞态条件
            f"math:{correct_answer}",
        )

        # choices 顺序即按钮顺序，展示文案由 render 层按 locale 渲染
        return MathChallenge(expression=expression, choices=tuple(options))

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
        chat_id: int, user_id: int, timeout: int = 60
    ) -> SliderChallenge:
        """生成滑块验证挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)
        """
        # 生成4个位置，只有一个是正确的（使用密码学安全的随机数）
        correct_position = secrets.randbelow(4)  # 0-3
        cells = ["⬜", "⬜", "⬜", "⬜"]
        cells[correct_position] = "🟩"

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout + 10,  # ✅ TTL 比超时时间多 10 秒，避免竞态条件
            f"slider:{correct_position}",  # 格式: slider:位置
        )

        return SliderChallenge(cells=tuple(cells))

    @staticmethod
    async def generate_qa_challenge(chat_id: int, user_id: int, timeout: int = 60) -> QAChallenge:
        """生成问答验证挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)
        """
        # 从题库随机选择一题
        qa = secrets.choice(QA_QUESTIONS)
        correct_index = qa.correct_index

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout + 10,  # ✅ TTL 比超时时间多 10 秒，避免竞态条件
            f"qa:{correct_index}",
        )

        # 题面/选项文案由 render 层用 question_id 按 locale 从 catalog 取（a/b/c/d 对应按钮 0-3）
        return QAChallenge(question_id=qa.id)

    @staticmethod
    async def generate_emoji_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> EmojiChallenge:
        """生成表情验证挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)
        """
        # 从映射表随机选择一个
        mapping = secrets.choice(EMOJI_MAPPINGS)
        correct_emoji = mapping.correct
        decoys = mapping.decoys

        # 组合所有选项并打乱
        all_emojis = [correct_emoji, *decoys]
        for i in range(len(all_emojis) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            all_emojis[i], all_emojis[j] = all_emojis[j], all_emojis[i]

        # 找到正确答案的新位置
        correct_index = all_emojis.index(correct_emoji)

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout + 10,  # ✅ TTL 比超时时间多 10 秒，避免竞态条件
            f"emoji:{correct_index}",
        )

        # 描述文案由 render 层用 description_id 按 locale 从 catalog 取
        return EmojiChallenge(description_id=mapping.id, emojis=tuple(all_emojis))

    @staticmethod
    async def generate_captcha_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> CaptchaChallenge:
        """生成图片验证码挑战

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
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

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout + 10,  # ✅ TTL 比超时时间多 10 秒，避免竞态条件
            f"captcha:{captcha_text.upper()}",
        )

        return CaptchaChallenge(photo=photo)

    @staticmethod
    async def generate_honeypot_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> HoneypotChallenge:
        """生成蜜罐验证挑战

        蜜罐验证包含一个数学题和诱饵按钮，机器人可能会点击诱饵

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)
        """
        # 生成简单的加法题
        num1 = secrets.randbelow(10) + 1
        num2 = secrets.randbelow(10) + 1
        correct_answer = num1 + num2

        # 诱饵用稳定 code，renderer 按 locale 映射展示文本（如"✅ 跳过验证"）
        decoy = secrets.choice(_HONEYPOT_DECOYS)

        # 生成错误答案
        wrong_answers: list[int] = []
        while len(wrong_answers) < 2:
            wrong = secrets.randbelow(20) + 1
            if wrong != correct_answer and wrong not in wrong_answers:
                wrong_answers.append(wrong)

        # 存储验证状态和答案到 Redis
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(
            key,
            timeout + 10,  # ✅ TTL 比超时时间多 10 秒，避免竞态条件
            f"honeypot:{correct_answer}",
        )

        # 选项顺序即按钮顺序（第二行三个真实选项）
        return HoneypotChallenge(
            expression=f"{num1} + {num2}",
            choices=(wrong_answers[0], correct_answer, wrong_answers[1]),
            decoy=decoy,
        )

    @staticmethod
    async def generate_puzzle_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> PuzzleChallenge:
        """生成拼图验证挑战

        用户需要选择灰色缺口的正确位置

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)
        """
        from PIL import Image, ImageDraw

        # 1. 生成背景图（280x100，随机渐变色）
        width, height = 280, 100
        img = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(img)

        # 随机渐变色
        color1 = (
            secrets.randbelow(200) + 55,
            secrets.randbelow(200) + 55,
            secrets.randbelow(200) + 55,
        )
        color2 = (
            secrets.randbelow(200) + 55,
            secrets.randbelow(200) + 55,
            secrets.randbelow(200) + 55,
        )

        for x in range(width):
            r = int(color1[0] + (color2[0] - color1[0]) * x / width)
            g = int(color1[1] + (color2[1] - color1[1]) * x / width)
            b = int(color1[2] + (color2[2] - color1[2]) * x / width)
            draw.line([(x, 0), (x, height)], fill=(r, g, b))

        # 2. 定义 4 个候选位置（拼图块大小 50x50）
        piece_size = 50
        positions = [
            (15, 25),  # 位置 1
            (80, 25),  # 位置 2
            (145, 25),  # 位置 3
            (210, 25),  # 位置 4
        ]

        # 3. 随机选择正确位置
        correct_idx = secrets.randbelow(4)
        correct_pos = positions[correct_idx]

        # 4. 在正确位置画缺口（白色/灰色方块）
        draw.rectangle(
            [correct_pos, (correct_pos[0] + piece_size, correct_pos[1] + piece_size)],
            fill=(200, 200, 200),
            outline=(150, 150, 150),
            width=2,
        )

        # 5. 在其他位置画装饰（让图片更丰富）
        for i, pos in enumerate(positions):
            if i != correct_idx:
                # 画一些随机形状
                shape_color = (
                    secrets.randbelow(100) + 100,
                    secrets.randbelow(100) + 100,
                    secrets.randbelow(100) + 100,
                )
                draw.ellipse([pos[0] + 10, pos[1] + 10, pos[0] + 40, pos[1] + 40], fill=shape_color)

        # 6. 转换为 BufferedInputFile
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        photo = BufferedInputFile(img_bytes.read(), filename="puzzle.png")

        # 7. 存储答案到 Redis（选项为固定 4 个位置，由 renderer 渲染按钮）
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(key, timeout + 10, f"puzzle:{correct_idx}")  # ✅ TTL 比超时时间多 10 秒

        return PuzzleChallenge(photo=photo)

    @staticmethod
    async def generate_turnstile_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> WebAppChallenge:
        """生成 Turnstile 验证挑战

        使用统一 CAPTCHA WebApp 完成 Cloudflare Turnstile 人机验证

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)

        Raises:
            ValueError: 未配置 CAPTCHA_WEBAPP_URL
        """
        from src.core.config import settings

        if not settings.captcha_webapp_url:
            raise ValueError(
                "Turnstile 验证需要配置 CAPTCHA_WEBAPP_URL\n"
                "请部署统一 CAPTCHA WebApp 并设置环境变量"
            )

        # 生成一次性 verify_token（防止重放攻击）
        verify_token = secrets.token_urlsafe(32)

        redis = get_redis()

        # 存储 token 到 Redis（统一格式：provider:token）
        token_key = RedisKeys.captcha_token(chat_id, user_id)
        await redis.setex(token_key, timeout + 10, f"turnstile:{verify_token}")

        # 构建 WebApp URL（使用独立页面）
        webapp_url = (
            f"{settings.captcha_webapp_url}/turnstile.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
        )

        # 存储验证状态
        verify_key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(verify_key, timeout + 10, "turnstile:pending")

        return WebAppChallenge(provider="turnstile", webapp_url=webapp_url)

    @staticmethod
    async def generate_friendly_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> WebAppChallenge:
        """生成 Friendly Captcha 验证挑战

        使用 Friendly Captcha 进行隐私友好的人机验证
        支持多 key 轮换，通过 Redis INCR 实现 round-robin 分配

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)

        Raises:
            ValueError: Friendly Captcha 未配置或未启用
        """
        from src.core.config import settings

        # 验证配置
        if not settings.friendly_enabled or not settings.friendly_keys:
            raise ValueError("Friendly Captcha 未配置或未启用")

        # 生成一次性 verify_token
        verify_token = secrets.token_urlsafe(32)

        # 使用 Redis INCR 实现 key 轮换（原子操作）
        redis = get_redis()
        index_key = RedisKeys.friendly_key_index()
        current_index = await redis.incr(index_key)
        key_index = (current_index - 1) % len(settings.friendly_keys)

        # 存储 token 到 Redis（包含 key_index）
        token_key = RedisKeys.captcha_token(chat_id, user_id)
        await redis.setex(token_key, timeout + 10, f"friendly:{verify_token}:{key_index}")

        # 存储验证状态
        verify_key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(verify_key, timeout + 10, "friendly:pending")

        # 构建 WebApp URL（使用独立页面）
        webapp_url = (
            f"{settings.captcha_webapp_url}/friendly.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}&key_index={key_index}"
        )

        return WebAppChallenge(provider="friendly", webapp_url=webapp_url)

    @staticmethod
    async def generate_hcaptcha_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> WebAppChallenge:
        """生成 hCaptcha 验证挑战

        使用 hCaptcha 进行图片验证
        注意：免费版需要用户手动完成图片识别任务

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)

        Raises:
            ValueError: hCaptcha 未配置或未启用
        """
        from src.core.config import settings

        # 验证配置
        if not settings.hcaptcha_enabled or not settings.hcaptcha_site_key:
            raise ValueError("hCaptcha 未配置或未启用")

        # 生成一次性 verify_token
        verify_token = secrets.token_urlsafe(32)

        # 存储 token 到 Redis
        redis = get_redis()
        token_key = RedisKeys.captcha_token(chat_id, user_id)
        await redis.setex(token_key, timeout + 10, f"hcaptcha:{verify_token}")

        # 存储验证状态
        verify_key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(verify_key, timeout + 10, "hcaptcha:pending")

        # 构建 WebApp URL（使用独立页面）
        webapp_url = (
            f"{settings.captcha_webapp_url}/hcaptcha.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
        )

        return WebAppChallenge(provider="hcaptcha", webapp_url=webapp_url)

    @staticmethod
    async def generate_mtcaptcha_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> WebAppChallenge:
        """生成 MTCaptcha 验证挑战

        使用 MTCaptcha 进行自适应无感验证

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)

        Raises:
            ValueError: MTCaptcha 未配置或未启用
        """
        from src.core.config import settings

        # 验证配置
        if not settings.mtcaptcha_enabled or not settings.mtcaptcha_site_key:
            raise ValueError("MTCaptcha 未配置或未启用")

        # 生成一次性 verify_token
        verify_token = secrets.token_urlsafe(32)

        # 存储 token 到 Redis
        redis = get_redis()
        token_key = RedisKeys.captcha_token(chat_id, user_id)
        await redis.setex(token_key, timeout + 10, f"mtcaptcha:{verify_token}")

        # 存储验证状态
        verify_key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(verify_key, timeout + 10, "mtcaptcha:pending")

        # 构建 WebApp URL（使用独立页面）
        webapp_url = (
            f"{settings.captcha_webapp_url}/mtcaptcha.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
        )

        return WebAppChallenge(provider="mtcaptcha", webapp_url=webapp_url)

    @staticmethod
    async def generate_altcha_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> WebAppChallenge:
        """生成 ALTCHA 验证挑战

        使用 ALTCHA 进行开源 Proof-of-Work 验证
        注意：需要独立部署 PHP 后端到 Serv00

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)

        Raises:
            ValueError: ALTCHA 未配置或未启用
        """
        from src.core.config import settings

        # 验证配置
        if not settings.altcha_enabled or not settings.altcha_api_url:
            raise ValueError("ALTCHA 未配置或未启用")

        # 生成一次性 verify_token
        verify_token = secrets.token_urlsafe(32)

        # 存储 token 到 Redis
        redis = get_redis()
        token_key = RedisKeys.captcha_token(chat_id, user_id)
        await redis.setex(token_key, timeout + 10, f"altcha:{verify_token}")

        # 存储验证状态
        verify_key = RedisKeys.verification(chat_id, user_id)
        await redis.setex(verify_key, timeout + 10, "altcha:pending")

        # 构建 WebApp URL（使用独立页面）
        webapp_url = (
            f"{settings.captcha_webapp_url}/altcha.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
        )

        return WebAppChallenge(provider="altcha", webapp_url=webapp_url)

    @staticmethod
    async def generate_random_challenge(
        chat_id: int, user_id: int, timeout: int = 60
    ) -> VerificationChallenge:
        """生成随机类型的验证挑战

        从可用的验证类型中随机选择一种
        自动检测哪些 CAPTCHA 服务已启用

        Args:
            chat_id: 群组 ID
            user_id: 用户 ID
            timeout: 验证超时时间(秒)
        """
        from src.core.config import settings

        # 始终可用的验证类型（不需要外部服务）
        available_types = ["math", "slider", "qa", "emoji", "captcha", "honeypot", "puzzle"]

        # 动态检测已启用的 CAPTCHA 服务
        if settings.turnstile_enabled and settings.turnstile_site_key:
            available_types.append("turnstile")

        if settings.friendly_enabled and settings.friendly_keys:
            available_types.append("friendly")

        if settings.hcaptcha_enabled and settings.hcaptcha_site_key:
            available_types.append("hcaptcha")

        if settings.mtcaptcha_enabled and settings.mtcaptcha_site_key:
            available_types.append("mtcaptcha")

        if settings.altcha_enabled and settings.altcha_api_url:
            available_types.append("altcha")

        # 随机选择一种类型
        selected_type = secrets.choice(available_types)

        # 根据选择的类型生成对应的挑战
        generators: dict[str, Callable[[int, int, int], Awaitable[VerificationChallenge]]] = {
            "math": VerificationService.generate_math_challenge,
            "slider": VerificationService.generate_slider_challenge,
            "qa": VerificationService.generate_qa_challenge,
            "emoji": VerificationService.generate_emoji_challenge,
            "captcha": VerificationService.generate_captcha_challenge,
            "puzzle": VerificationService.generate_puzzle_challenge,
            "honeypot": VerificationService.generate_honeypot_challenge,
            "turnstile": VerificationService.generate_turnstile_challenge,
            "friendly": VerificationService.generate_friendly_challenge,
            "hcaptcha": VerificationService.generate_hcaptcha_challenge,
            "mtcaptcha": VerificationService.generate_mtcaptcha_challenge,
            "altcha": VerificationService.generate_altcha_challenge,
        }
        return await generators[selected_type](chat_id, user_id, timeout)

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
        """清除验证状态（包括验证类型标记）"""
        redis = get_redis()
        # 清除主验证 key
        key = RedisKeys.verification(chat_id, user_id)
        await redis.delete(key)
        # 清除验证类型标记
        type_key = RedisKeys.verification_type(chat_id, user_id)
        await redis.delete(type_key)

    @staticmethod
    async def is_verification_pending(chat_id: int, user_id: int) -> bool:
        """检查是否有待验证状态"""
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        return await redis.exists(key) > 0
