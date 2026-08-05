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
from typing import Literal
from urllib.parse import quote

from aiogram.types import BufferedInputFile
from captcha.image import ImageCaptcha

from src.core.redis import RedisKeys, get_redis
from src.data.verification.emoji_mapping import EMOJI_MAPPINGS
from src.data.verification.qa_questions import QA_QUESTIONS
from src.services.verification_recovery import (
    VERIFICATION_GRACE_MS,
    RecoveryReservation,
    VerificationClearToken,
    VerificationFlow,
    capture_verification_clear_token,
    claim_failure,
    claim_success,
    clear_verification_state,
    commit_recovery,
)

# WebApp 验证的 5 个 provider 共用 WebAppChallenge，用 provider 字段区分
type WebAppProvider = Literal["turnstile", "friendly", "hcaptcha", "mtcaptcha", "altcha"]
# 所有可持久化的具体验证类型（random 在 prepare 阶段解析为具体类型，永不落库）
type ConcreteVerificationType = Literal[
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
]
# 蜜罐诱饵用稳定 code（非中文文案），renderer 按 locale 映射展示文本
type HoneypotDecoy = Literal["skip", "direct", "human"]
_HONEYPOT_DECOYS: tuple[HoneypotDecoy, ...] = ("skip", "direct", "human")

# 选项类验证的校验结果：correct 正确 / wrong 错误（含 honeypot 陷阱）/ expired 已过期或类型不匹配
type ChoiceAnswerResult = Literal["correct", "wrong", "expired"]


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """答案校验结果；correct/wrong 携带对应原子 claim 返回的 flow。

    ``__post_init__`` 强制 correct/wrong 必须携带 flow、expired 不得携带 flow，
    捕获调用方的组合错误。
    """

    status: ChoiceAnswerResult
    flow: VerificationFlow | None = None

    def __post_init__(self) -> None:
        if self.flow is not None and self.flow not in ("join", "join_request"):
            raise ValueError("VerifyResult.flow 非法")
        if (self.status in ("correct", "wrong")) != (self.flow is not None):
            raise ValueError("VerifyResult: correct/wrong 必须携带 flow，expired 不得携带 flow")


# captcha 刷新专用 CAS：generator 拆分后 refresh 不能用旧 SETEX（会在 clear/timeout 后复活
# 状态），故校验主键为 captcha: 前缀、recovery 为 message:{session}:、deadline 未到，才原子
# 替换主键。KEYS = [main, deadline, recovery]。
_COMMIT_CAPTCHA_REFRESH_SCRIPT = """
local state_value = ARGV[1]
local grace_ms = tonumber(ARGV[2])

local deadline_raw = redis.call("get", KEYS[2])
local current_main = redis.call("get", KEYS[1])
if not deadline_raw or not current_main
    or string.sub(current_main, 1, 8) ~= "captcha:" then
    return 0
end

local separator = string.find(deadline_raw, ":", 1, true)
if not separator then
    return 0
end

local session = string.sub(deadline_raw, 1, separator - 1)
local deadline_ms = tonumber(string.sub(deadline_raw, separator + 1))
local recovery_raw = redis.call("get", KEYS[3])
local message_prefix = "message:" .. session .. ":"

if not deadline_ms or not recovery_raw
    or string.sub(recovery_raw, 1, string.len(message_prefix)) ~= message_prefix then
    return 0
end

local clock = redis.call("time")
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if now_ms >= deadline_ms then
    return 0
end

redis.call("set", KEYS[1], state_value)
redis.call("pexpireat", KEYS[1], deadline_ms + grace_ms)
return 1
""".strip()


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


@dataclass(frozen=True, slots=True)
class PreparedChallenge:
    """纯生成结果：challenge + 主键状态值 + WebApp auxiliary token。

    只有 ``commit_challenge`` 可把 state_value/auxiliary_state 写入正式 verification 状态键，
    保证 prepare 阶段无 Redis 副作用（恢复路径可安全重试）。
    """

    challenge: VerificationChallenge
    state_value: str
    auxiliary_state: str | None = None


class VerificationService:
    """入群验证服务"""

    @staticmethod
    def _prepare_math_challenge() -> PreparedChallenge:
        """纯生成数学验证码挑战（支持四则运算，最多两步）。"""
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

        # choices 顺序即按钮顺序，展示文案由 render 层按 locale 渲染
        return PreparedChallenge(
            challenge=MathChallenge(expression=expression, choices=tuple(options)),
            state_value=f"math:{correct_answer}",
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
    def _prepare_slider_challenge() -> PreparedChallenge:
        """纯生成滑块验证挑战。"""
        # 生成4个位置，只有一个是正确的（使用密码学安全的随机数）
        correct_position = secrets.randbelow(4)  # 0-3
        cells = ["⬜", "⬜", "⬜", "⬜"]
        cells[correct_position] = "🟩"

        return PreparedChallenge(
            challenge=SliderChallenge(cells=tuple(cells)),
            state_value=f"slider:{correct_position}",
        )

    @staticmethod
    def _prepare_qa_challenge() -> PreparedChallenge:
        """纯生成问答验证挑战。"""
        # 从题库随机选择一题；题面/选项文案由 render 层用 question_id 按 locale 从 catalog 取
        # （a/b/c/d 对应按钮 0-3）
        qa = secrets.choice(QA_QUESTIONS)
        return PreparedChallenge(
            challenge=QAChallenge(question_id=qa.id),
            state_value=f"qa:{qa.correct_index}",
        )

    @staticmethod
    def _prepare_emoji_challenge() -> PreparedChallenge:
        """纯生成表情验证挑战。"""
        # 从映射表随机选择一个
        mapping = secrets.choice(EMOJI_MAPPINGS)
        correct_emoji = mapping.correct
        decoys = mapping.decoys

        # 组合所有选项并打乱
        all_emojis = [correct_emoji, *decoys]
        for i in range(len(all_emojis) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            all_emojis[i], all_emojis[j] = all_emojis[j], all_emojis[i]

        # 找到正确答案的新位置；描述文案由 render 层用 description_id 按 locale 从 catalog 取
        correct_index = all_emojis.index(correct_emoji)
        return PreparedChallenge(
            challenge=EmojiChallenge(description_id=mapping.id, emojis=tuple(all_emojis)),
            state_value=f"emoji:{correct_index}",
        )

    @staticmethod
    def _prepare_captcha_challenge() -> PreparedChallenge:
        """纯生成图片验证码挑战。"""
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

        return PreparedChallenge(
            challenge=CaptchaChallenge(photo=photo),
            state_value=f"captcha:{captcha_text.upper()}",
        )

    @staticmethod
    def _prepare_honeypot_challenge() -> PreparedChallenge:
        """纯生成蜜罐验证挑战（数学题 + 诱饵按钮，机器人可能点击诱饵）。"""
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

        # 选项顺序即按钮顺序（第二行三个真实选项）
        return PreparedChallenge(
            challenge=HoneypotChallenge(
                expression=f"{num1} + {num2}",
                choices=(wrong_answers[0], correct_answer, wrong_answers[1]),
                decoy=decoy,
            ),
            state_value=f"honeypot:{correct_answer}",
        )

    @staticmethod
    def _prepare_puzzle_challenge() -> PreparedChallenge:
        """纯生成拼图验证挑战（用户选择灰色缺口的正确位置）。"""
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

        # 选项为固定 4 个位置，由 renderer 渲染按钮
        return PreparedChallenge(
            challenge=PuzzleChallenge(photo=photo),
            state_value=f"puzzle:{correct_idx}",
        )

    @staticmethod
    def _prepare_turnstile_challenge(
        chat_id: int, user_id: int, *, locale: str
    ) -> PreparedChallenge:
        """纯生成 Turnstile WebApp 挑战（token 由 commit 同事务写入 captcha_token）。"""
        from src.core.config import settings

        if not settings.captcha_webapp_url:
            raise ValueError(
                "Turnstile 验证需要配置 CAPTCHA_WEBAPP_URL\n"
                "请部署统一 CAPTCHA WebApp 并设置环境变量"
            )

        # 生成一次性 verify_token（防止重放攻击）；写入推迟到 commit 同事务
        verify_token = secrets.token_urlsafe(32)
        webapp_url = (
            f"{settings.captcha_webapp_url}/turnstile.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
            f"&locale={quote(locale, safe='')}"
        )

        return PreparedChallenge(
            challenge=WebAppChallenge(provider="turnstile", webapp_url=webapp_url),
            state_value="turnstile:pending",
            auxiliary_state=f"turnstile:{verify_token}",
        )

    @staticmethod
    def _prepare_friendly_challenge(
        chat_id: int, user_id: int, *, locale: str
    ) -> PreparedChallenge:
        """纯生成 Friendly Captcha 挑战。

        原实现用 Redis INCR 做 key 轮换，但 INCR 是 Redis 写，与「prepare 无副作用」冲突。
        改用 secrets.randbelow 均匀选 key——失去严格 round-robin，但保证 prepare 纯函数
        （恢复可安全重试，commit 失败不留全局副作用）。如需严格轮换，应独立成另一原子
        状态，不可藏在 pure prepare 中。
        """
        from src.core.config import settings

        if not settings.friendly_enabled or not settings.friendly_keys:
            raise ValueError("Friendly Captcha 未配置或未启用")

        verify_token = secrets.token_urlsafe(32)
        key_index = secrets.randbelow(len(settings.friendly_keys))
        webapp_url = (
            f"{settings.captcha_webapp_url}/friendly.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}&key_index={key_index}"
            f"&locale={quote(locale, safe='')}"
        )

        return PreparedChallenge(
            challenge=WebAppChallenge(provider="friendly", webapp_url=webapp_url),
            state_value="friendly:pending",
            auxiliary_state=f"friendly:{verify_token}:{key_index}",
        )

    @staticmethod
    def _prepare_hcaptcha_challenge(
        chat_id: int, user_id: int, *, locale: str
    ) -> PreparedChallenge:
        """纯生成 hCaptcha 挑战。"""
        from src.core.config import settings

        if not settings.hcaptcha_enabled or not settings.hcaptcha_site_key:
            raise ValueError("hCaptcha 未配置或未启用")

        verify_token = secrets.token_urlsafe(32)
        webapp_url = (
            f"{settings.captcha_webapp_url}/hcaptcha.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
            f"&locale={quote(locale, safe='')}"
        )

        return PreparedChallenge(
            challenge=WebAppChallenge(provider="hcaptcha", webapp_url=webapp_url),
            state_value="hcaptcha:pending",
            auxiliary_state=f"hcaptcha:{verify_token}",
        )

    @staticmethod
    def _prepare_mtcaptcha_challenge(
        chat_id: int, user_id: int, *, locale: str
    ) -> PreparedChallenge:
        """纯生成 MTCaptcha 挑战。"""
        from src.core.config import settings

        if not settings.mtcaptcha_enabled or not settings.mtcaptcha_site_key:
            raise ValueError("MTCaptcha 未配置或未启用")

        verify_token = secrets.token_urlsafe(32)
        webapp_url = (
            f"{settings.captcha_webapp_url}/mtcaptcha.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
            f"&locale={quote(locale, safe='')}"
        )

        return PreparedChallenge(
            challenge=WebAppChallenge(provider="mtcaptcha", webapp_url=webapp_url),
            state_value="mtcaptcha:pending",
            auxiliary_state=f"mtcaptcha:{verify_token}",
        )

    @staticmethod
    def _prepare_altcha_challenge(chat_id: int, user_id: int, *, locale: str) -> PreparedChallenge:
        """纯生成 ALTCHA 挑战。"""
        from src.core.config import settings

        if not settings.altcha_enabled or not settings.altcha_api_url:
            raise ValueError("ALTCHA 未配置或未启用")

        verify_token = secrets.token_urlsafe(32)
        webapp_url = (
            f"{settings.captcha_webapp_url}/altcha.html"
            f"?chat_id={chat_id}&user_id={user_id}"
            f"&token={verify_token}"
            f"&locale={quote(locale, safe='')}"
        )

        return PreparedChallenge(
            challenge=WebAppChallenge(provider="altcha", webapp_url=webapp_url),
            state_value="altcha:pending",
            auxiliary_state=f"altcha:{verify_token}",
        )

    @staticmethod
    def resolve_challenge_type(challenge_type: str) -> ConcreteVerificationType:
        """把 random/未知配置解析成具体类型；random 永不落库（主键存具体前缀）。"""
        from src.core.config import settings

        concrete_types: tuple[ConcreteVerificationType, ...] = (
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
        )
        if challenge_type in concrete_types:
            return challenge_type
        if challenge_type != "random":
            # 未知类型默认数学验证
            return "math"

        # random：从已启用的类型中随机选（始终可用 7 类 + 已启用 CAPTCHA 服务）
        available: list[ConcreteVerificationType] = [
            "math",
            "slider",
            "qa",
            "emoji",
            "captcha",
            "honeypot",
            "puzzle",
        ]
        if settings.turnstile_enabled and settings.turnstile_site_key:
            available.append("turnstile")
        if settings.friendly_enabled and settings.friendly_keys:
            available.append("friendly")
        if settings.hcaptcha_enabled and settings.hcaptcha_site_key:
            available.append("hcaptcha")
        if settings.mtcaptcha_enabled and settings.mtcaptcha_site_key:
            available.append("mtcaptcha")
        if settings.altcha_enabled and settings.altcha_api_url:
            available.append("altcha")
        return secrets.choice(available)

    @staticmethod
    async def prepare_challenge(
        challenge_type: str,
        chat_id: int,
        user_id: int,
        *,
        locale: str,
    ) -> PreparedChallenge:
        """纯 prepare：按 challenge_type 生成展示数据 + 待提交状态，不写正式 Redis 键。

        random 在此解析为具体类型，主键永远存具体前缀（math:* 等），不存 random:*。
        ``locale`` 仅 WebApp 类型用于拼接页面语言参数（非 WebApp 类型忽略）。
        """
        concrete = VerificationService.resolve_challenge_type(challenge_type)

        # 纯生成类型（无需 chat_id/user_id）
        if concrete == "math":
            return VerificationService._prepare_math_challenge()
        if concrete == "slider":
            return VerificationService._prepare_slider_challenge()
        if concrete == "qa":
            return VerificationService._prepare_qa_challenge()
        if concrete == "emoji":
            return VerificationService._prepare_emoji_challenge()
        if concrete == "captcha":
            return VerificationService._prepare_captcha_challenge()
        if concrete == "honeypot":
            return VerificationService._prepare_honeypot_challenge()
        if concrete == "puzzle":
            return VerificationService._prepare_puzzle_challenge()

        # WebApp 类型（构建 URL 需要 chat_id/user_id；locale 注入页面语言）
        if concrete == "turnstile":
            return VerificationService._prepare_turnstile_challenge(chat_id, user_id, locale=locale)
        if concrete == "friendly":
            return VerificationService._prepare_friendly_challenge(chat_id, user_id, locale=locale)
        if concrete == "hcaptcha":
            return VerificationService._prepare_hcaptcha_challenge(chat_id, user_id, locale=locale)
        if concrete == "mtcaptcha":
            return VerificationService._prepare_mtcaptcha_challenge(chat_id, user_id, locale=locale)
        return VerificationService._prepare_altcha_challenge(chat_id, user_id, locale=locale)

    @staticmethod
    async def commit_challenge(
        chat_id: int,
        user_id: int,
        prepared: PreparedChallenge,
        session_id: str,
        deadline_ms: int,
        flow: VerificationFlow,
        *,
        reservation: RecoveryReservation,
    ) -> bool:
        """按 reservation owner/旧主键 CAS 提交 challenge 状态。

        reservation 不能省略：session/deadline 本身不足以区分同一 session 下并发的
        恢复 revision/owner。校验 reservation 与显式参数一致后委托 commit_recovery。
        """
        if (
            reservation.chat_id != chat_id
            or reservation.user_id != user_id
            or reservation.session_id != session_id
            or reservation.deadline_ms != deadline_ms
        ):
            return False

        return await commit_recovery(
            reservation,
            state_value=prepared.state_value,
            auxiliary_state=prepared.auxiliary_state,
            flow=flow,
        )

    @staticmethod
    async def commit_captcha_refresh(
        chat_id: int,
        user_id: int,
        prepared: PreparedChallenge,
    ) -> bool:
        """刷新 captcha：仅在当前 session/message/deadline 仍有效时替换答案。

        generator 拆分后 refresh 不能用旧 SETEX（会在 clear/timeout 后复活状态），用独立
        Lua CAS：校验主键为 captcha: 前缀、recovery 为 message:{session}:、deadline 未到，
        才原子替换主键。
        """
        if not isinstance(prepared.challenge, CaptchaChallenge):
            raise TypeError("prepared 必须是 CaptchaChallenge")
        if not prepared.state_value.startswith("captcha:"):
            raise ValueError("captcha state_value 格式错误")

        redis = get_redis()
        committed = await redis.eval(
            _COMMIT_CAPTCHA_REFRESH_SCRIPT,
            3,
            RedisKeys.verification(chat_id, user_id),
            RedisKeys.verification_deadline(chat_id, user_id),
            RedisKeys.verification_recovery(chat_id, user_id),
            prepared.state_value,
            VERIFICATION_GRACE_MS,
        )
        return bool(committed)

    @staticmethod
    async def verify_choice_answer(
        chat_id: int, user_id: int, expected_type: str, answer: str
    ) -> VerifyResult:
        """读取答案快照，按答案对错用 Lua 原子 claim 成功/失败路径（消除 TOCTOU）。

        MGET 一次读 main + deadline 快照；类型不匹配视为过期消息（旧类型按钮点击当前
        挑战），避免跨类型旧消息碰巧通过当前挑战的 stored 答案。

        correct/wrong 携带对应 claim 原子返回的 flow；expired 表示状态无效或 claim 失败
        （timeout 已接管或 session 切换），调用方静默退出且不处罚。
        """
        redis = get_redis()
        stored_value, deadline_value = await redis.mget(
            RedisKeys.verification(chat_id, user_id),
            RedisKeys.verification_deadline(chat_id, user_id),
        )
        if not stored_value or not deadline_value:
            return VerifyResult(status="expired")

        # partition 校验：type 与 answer 均非空，且 answer 不含冒号
        # （防损坏值如 qa: / :2 / qa:2:extra 误判）
        challenge_type, sep, correct_answer = stored_value.partition(":")
        if not sep or not challenge_type or not correct_answer or ":" in correct_answer:
            return VerifyResult(status="expired")

        if challenge_type != expected_type:
            return VerifyResult(status="expired")

        if answer == "trap" or answer != correct_answer:
            # 答案错误（含 honeypot trap）：原子 claim 失败路径，先删 main 使后续 timeout
            # claim 在 GET 阶段即 stale，消除 ban/decline 经网络卡入 grace 期时重复处罚
            flow = await claim_failure(chat_id, user_id, stored_value, deadline_value)
            if flow is None:
                return VerifyResult(status="expired")
            return VerifyResult(status="wrong", flow=flow)

        # 答案正确：原子 claim（双 CAS 防 ABA，与 timeout 互斥）——成功返回 flow
        flow = await claim_success(chat_id, user_id, stored_value, deadline_value)
        if flow is None:
            return VerifyResult(status="expired")
        return VerifyResult(status="correct", flow=flow)

    @staticmethod
    async def verify_answer(
        chat_id: int,
        user_id: int,
        answer: str,
        *,
        expected_deadline_value: str,
    ) -> VerifyResult:
        """验证 captcha 文本答案，按对错用 Lua 原子 claim 并返回 flow。

        仅服务 captcha 文本输入路径（大小写不敏感）。``expected_deadline_value`` 把 waiting
        快照绑定到本次 MGET，防 handler 校验 waiting 后 session 切换、再用新 main 判定旧输入。
        结果语义同 verify_choice_answer。
        """
        redis = get_redis()
        stored_value, deadline_value = await redis.mget(
            RedisKeys.verification(chat_id, user_id),
            RedisKeys.verification_deadline(chat_id, user_id),
        )
        if not stored_value or not deadline_value:
            return VerifyResult(status="expired")
        if deadline_value != expected_deadline_value:
            return VerifyResult(status="expired")

        # partition 校验：必须是 captcha:{大写文本}，文本非空且不含冒号（防损坏值）
        challenge_type, sep, correct_answer = stored_value.partition(":")
        if not sep or challenge_type != "captcha" or not correct_answer or ":" in correct_answer:
            return VerifyResult(status="expired")

        if answer.upper() != correct_answer.upper():
            # 答案错误：原子 claim 失败路径（同 verify_choice_answer，与 timeout 互斥）
            flow = await claim_failure(chat_id, user_id, stored_value, deadline_value)
            if flow is None:
                return VerifyResult(status="expired")
            return VerifyResult(status="wrong", flow=flow)

        # 答案正确：原子 claim（双 CAS 防 ABA，与 timeout 互斥）——成功返回 flow
        flow = await claim_success(chat_id, user_id, stored_value, deadline_value)
        if flow is None:
            return VerifyResult(status="expired")
        return VerifyResult(status="correct", flow=flow)

    @staticmethod
    async def capture_clear_token(chat_id: int, user_id: int) -> VerificationClearToken:
        """取得 clear CAS 快照。延迟清理路径必须在 Telegram 调用前取得。"""
        return await capture_verification_clear_token(chat_id, user_id)

    @staticmethod
    async def clear_verification(
        chat_id: int,
        user_id: int,
        *,
        expected: VerificationClearToken | None = None,
    ) -> bool:
        """按 session 快照清除状态。

        ``expected=None`` 内部捕获当前快照，仅适合无外部等待的"清理当前状态"路径（管理员/
        恢复）；处罚/成功等延迟路径必须在 Telegram 副作用前 capture 并传入 ``expected``，
        避免旧协程把新 session 当当前 session 误删。返回是否实际删除。
        """
        if expected is None:
            expected = await capture_verification_clear_token(chat_id, user_id)
        return await clear_verification_state(chat_id, user_id, expected)

    @staticmethod
    async def is_verification_pending(chat_id: int, user_id: int) -> bool:
        """检查是否有待验证状态"""
        redis = get_redis()
        key = RedisKeys.verification(chat_id, user_id)
        return await redis.exists(key) > 0
