"""OCR 提供者抽象层和具体实现

支持多种 OCR 提供者：
- Gemini AI OCR（云 API，第一优先级）
- 百度智能云 OCR（云 API，第二优先级）
- PaddleOCR（本地，轻量级）
- EasyOCR（本地，当前实现）
"""

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from src.core.executor import run_in_executor

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class OCRResult:
    """OCR 提取结果"""

    text: str  # 提取的文本
    provider: str  # 提供者名称
    confidence: float  # 置信度 (0.0-1.0)


@dataclass
class ProviderStats:
    """提供者统计信息"""

    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_failure_time: datetime | None = None
    last_error: str = ""

    def record_success(self) -> None:
        """记录成功"""
        self.success_count += 1
        self.consecutive_failures = 0

    def record_failure(self, error: str) -> None:
        """记录失败"""
        self.failure_count += 1
        self.consecutive_failures += 1
        self.last_failure_time = datetime.now()
        self.last_error = error

    def reset_circuit(self) -> None:
        """重置熔断器"""
        self.consecutive_failures = 0
        self.last_failure_time = None


# ============================================================================
# 异常类
# ============================================================================


class OCRProviderError(Exception):
    """OCR 提供者基础异常"""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"[{provider}] {message}")


class OCRProviderConfigError(OCRProviderError):
    """OCR 提供者配置错误"""

    pass


class OCRProviderRuntimeError(OCRProviderError):
    """OCR 提供者运行时错误"""

    pass


# ============================================================================
# OCR 提供者抽象基类
# ============================================================================


class OCRProvider(ABC):
    """OCR 提供者抽象基类

    所有 OCR 实现必须实现此接口
    """

    def __init__(self, name: str):
        """初始化提供者

        Args:
            name: 提供者名称（用于日志和统计）
        """
        self.name = name

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """检查提供者是否可用

        Returns:
            True 如果提供者已正确配置且可用
        """
        pass

    @abstractmethod
    async def extract(self, image_path: str) -> OCRResult | None:
        """从图片中提取文本

        Args:
            image_path: 图片文件路径

        Returns:
            OCRResult 如果成功，None 如果失败或未检测到文本

        Raises:
            OCRProviderRuntimeError: 提取过程中发生错误
        """
        pass

    @abstractmethod
    async def extract_with_details(self, image_path: str) -> list[tuple[str, float]] | None:
        """从图片中提取文本（带详细信息）

        Args:
            image_path: 图片文件路径

        Returns:
            [(文本, 置信度), ...] 列表，如果失败返回 None

        Raises:
            OCRProviderRuntimeError: 提取过程中发生错误
        """
        pass

    def _validate_image_path(self, image_path: str) -> Path:
        """验证图片路径

        Args:
            image_path: 图片路径

        Returns:
            验证后的 Path 对象

        Raises:
            OCRProviderRuntimeError: 路径无效
        """
        path_obj = Path(image_path).resolve()

        # 检查文件是否存在
        if not path_obj.exists():
            raise OCRProviderRuntimeError(self.name, f"图片文件不存在: {image_path}")

        # 检查是否是文件（不是目录）
        if not path_obj.is_file():
            raise OCRProviderRuntimeError(self.name, f"路径不是文件: {image_path}")

        # 检查文件扩展名是否为图片格式
        allowed_extensions = {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".webp",
        }
        if path_obj.suffix.lower() not in allowed_extensions:
            raise OCRProviderRuntimeError(self.name, f"不支持的图片格式: {path_obj.suffix}")

        return path_obj


# ============================================================================
# Gemini AI OCR 提供者
# ============================================================================


class OpenAIOCRProvider(OCRProvider):
    """OpenAI OCR 提供者

    特点:
    - 云 API，无本地内存占用
    - 多模态理解，可结合上下文
    - 支持 GPT-4o、GPT-4o-mini 等模型
    - 支持自定义 API Base URL（兼容接口）

    模型选择:
    - gpt-4o-mini（推荐）：快速、便宜
    - gpt-4o：高准确率
    - gpt-4-turbo：旧版本

    注意事项:
    - 使用 OpenAI Python SDK
    - 图片需要 base64 编码
    - 按使用量计费

    环境变量:
    - OCR_OPENAI_API_KEY: API Key（必填）
    - OCR_OPENAI_MODEL: 模型名称（默认：gpt-4o-mini）
    - OCR_OPENAI_API_URL: 自定义 API Base URL（可选，用于代理或兼容接口）
    - OCR_OPENAI_TIMEOUT: 超时时间（秒，默认：30）

    注册地址: https://platform.openai.com/api-keys
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = "gpt-4o-mini",
        api_url: str | None = None,
        timeout: int = 30,
    ):
        super().__init__("openai")
        self.api_key = api_key
        self.model_name = model_name
        self.api_url = api_url
        self.timeout = timeout

    @property
    def is_available(self) -> bool:
        """检查 OpenAI OCR 是否可用"""
        return bool(self.api_key)

    async def extract(self, image_path: str) -> OCRResult | None:
        """从图片中提取文本（使用 OpenAI Vision API）"""
        try:
            # 验证图片路径
            path_obj = self._validate_image_path(image_path)

            # 导入 OpenAI SDK
            import base64

            from openai import AsyncOpenAI

            # 创建客户端
            client_kwargs = {"api_key": self.api_key, "timeout": self.timeout}
            if self.api_url:
                client_kwargs["base_url"] = self.api_url

            client = AsyncOpenAI(**client_kwargs)

            # 读取图片并 base64 编码
            with open(path_obj, "rb") as f:
                image_data = f.read()
            image_base64 = base64.b64encode(image_data).decode("utf-8")

            # 检测图片 MIME 类型
            mime_type = self._get_mime_type(path_obj.suffix.lower())

            # 调用 OpenAI Vision API
            response = await client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an OCR assistant. Extract all text from the image accurately. Return only the extracted text without any additional explanation or commentary.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Extract all text from this image.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                            },
                        ],
                    },
                ],
                max_tokens=2000,
            )

            # 解析响应
            if response.choices and response.choices[0].message.content:
                full_text = response.choices[0].message.content.strip()

                if not full_text:
                    logger.debug("OpenAI OCR: 图片中未检测到文字")
                    return None

                logger.debug(
                    f"OpenAI OCR 提取成功 [字数:{len(full_text)}] "
                    f"[模型:{self.model_name}]"
                    + (f" [API URL:{self.api_url}]" if self.api_url else "")
                )

                return OCRResult(
                    text=full_text,
                    provider=self.name,
                    confidence=0.95,  # OpenAI 不返回置信度，使用默认值
                )
            else:
                logger.debug("OpenAI OCR: 未返回结果")
                return None

        except Exception as e:
            logger.error(f"OpenAI OCR 提取失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"提取失败: {e}") from e

    def _get_mime_type(self, suffix: str) -> str:
        """根据文件后缀获取 MIME 类型"""
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".webp": "image/webp",
        }
        return mime_types.get(suffix, "image/jpeg")

    async def extract_with_details(self, image_path: str) -> list[tuple[str, float]] | None:
        """从图片中提取文本（带详细信息）"""
        # OpenAI 不返回详细的置信度信息
        result = await self.extract(image_path)
        if result:
            # 简单的分句处理
            import re

            sentences = re.split(r"[。！？\n]", result.text)
            sentences = [s.strip() for s in sentences if s.strip()]
            # 假设所有句子置信度相同
            return [(s, 0.95) for s in sentences]
        return None


# ============================================================================
# 百度智能云 OCR 提供者
# ============================================================================


class BaiduOCRProvider(OCRProvider):
    """百度智能云 OCR 提供者

    特点:
    - 云 API，无本地内存占用
    - 准确率高，中文优化
    - 免费额度充足

    环境变量:
    - OCR_BAIDU_API_KEY: API Key（必填）
    - OCR_BAIDU_SECRET_KEY: Secret Key（必填）
    - OCR_BAIDU_USE_ACCURATE: 是否使用高精度版（默认：False）
    - OCR_BAIDU_TIMEOUT: 超时时间（秒，默认：10）

    免费额度:
    - 通用文字识别: 5000次/月
    - 高精度版: 500次/天
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        use_accurate: bool = False,
        timeout: int = 10,
    ):
        super().__init__("baidu")
        self.api_key = api_key
        self.secret_key = secret_key
        self.use_accurate = use_accurate
        self.timeout = timeout
        self._access_token: str | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def is_available(self) -> bool:
        """检查百度云 OCR 是否可用"""
        return bool(self.api_key and self.secret_key)

    async def _get_access_token(self) -> str:
        """获取 Access Token"""
        if self._access_token:
            return self._access_token

        # 调用百度 OAuth API
        url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            if "access_token" not in data:
                raise OCRProviderRuntimeError(self.name, f"获取 Access Token 失败: {data}")

            self._access_token = data["access_token"]
            return self._access_token

    async def extract(self, image_path: str) -> OCRResult | None:
        """从图片中提取文本（异步 HTTP 调用）"""
        try:
            # 验证图片路径
            path_obj = self._validate_image_path(image_path)

            # 获取 Access Token
            access_token = await self._get_access_token()

            # 读取图片并 base64 编码
            with open(path_obj, "rb") as f:
                image_data = f.read()
            image_base64 = base64.b64encode(image_data).decode("utf-8")

            # 选择 API 端点
            if self.use_accurate:
                api_url = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
            else:
                api_url = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"

            # 调用百度 OCR API
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    api_url,
                    data={"image": image_base64},
                    params={"access_token": access_token},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                result = response.json()

            # 解析响应
            if "error_code" in result:
                raise OCRProviderRuntimeError(
                    self.name, f"API 错误: {result.get('error_msg', 'Unknown error')}"
                )

            if "words_result" not in result or not result["words_result"]:
                logger.debug("百度云 OCR: 图片中未检测到文字")
                return None

            # 提取所有文本
            texts = [item["words"] for item in result["words_result"]]
            full_text = " ".join(texts)

            logger.debug(
                f"百度云 OCR 提取成功 [字数:{len(full_text)}] "
                f"[{'高精度' if self.use_accurate else '标准'}]"
            )

            return OCRResult(
                text=full_text,
                provider=self.name,
                confidence=0.95,  # 百度云不返回整体置信度
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"百度云 OCR HTTP 错误: {e}")
            raise OCRProviderRuntimeError(self.name, f"HTTP 错误: {e}") from e
        except Exception as e:
            logger.error(f"百度云 OCR 提取失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"提取失败: {e}") from e

    async def extract_with_details(self, image_path: str) -> list[tuple[str, float]] | None:
        """从图片中提取文本（带详细信息）"""
        try:
            # 验证图片路径
            path_obj = self._validate_image_path(image_path)

            # 获取 Access Token
            access_token = await self._get_access_token()

            # 读取图片并 base64 编码
            with open(path_obj, "rb") as f:
                image_data = f.read()
            image_base64 = base64.b64encode(image_data).decode("utf-8")

            # 选择 API 端点
            if self.use_accurate:
                api_url = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
            else:
                api_url = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"

            # 调用百度 OCR API
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    api_url,
                    data={"image": image_base64},
                    params={"access_token": access_token},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                result = response.json()

            # 解析响应
            if "error_code" in result:
                raise OCRProviderRuntimeError(
                    self.name, f"API 错误: {result.get('error_msg', 'Unknown error')}"
                )

            if "words_result" not in result or not result["words_result"]:
                return None

            # 提取文本和置信度（如果可用）
            texts_with_conf = []
            for item in result["words_result"]:
                text = item["words"]
                # 百度云标准版不返回置信度，使用默认值
                confidence = item.get("probability", 0.95)
                texts_with_conf.append((text, confidence))

            return texts_with_conf if texts_with_conf else None

        except Exception as e:
            logger.error(f"百度云 OCR 提取失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"提取失败: {e}") from e


# ============================================================================
# PaddleOCR 提供者
# ============================================================================


class PaddleOCRProvider(OCRProvider):
    """PaddleOCR 提供者

    特点:
    - 本地运行，内存占用 ~1-2GB
    - 速度快，准确率高
    - 模型仅 16MB（超轻量 PP-OCRv3）

    环境变量:
    - OCR_PADDLE_ENABLED: 是否启用（默认：True）
    - OCR_PADDLE_LANG: 语言（ch=中英文，en=英文，默认：ch）
    """

    def __init__(self, lang: str = "ch"):
        super().__init__("paddle")
        self.lang = lang
        self._reader: Any | None = None

    def _init_ocr(self) -> None:
        """延迟初始化 PaddleOCR"""
        if self._reader is not None:
            return

        try:
            from paddleocr import PaddleOCR

            logger.info("正在初始化 PaddleOCR...")

            self._reader = PaddleOCR(
                use_angle_cls=True,
                lang=self.lang,
                show_log=False,
            )

            logger.info("✅ PaddleOCR 初始化成功")

        except ImportError:
            logger.warning("PaddleOCR 未安装，安装命令: pip install paddleocr paddlepaddle")
            raise OCRProviderConfigError(self.name, "PaddleOCR 未安装") from None
        except Exception as e:
            logger.error(f"PaddleOCR 初始化失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"初始化失败: {e}") from e

    @property
    def is_available(self) -> bool:
        """检查 PaddleOCR 是否可用"""
        try:
            self._init_ocr()
            return True
        except Exception:
            return False

    async def extract(self, image_path: str) -> OCRResult | None:
        """从图片中提取文本（在线程池中执行）"""
        try:
            # 验证图片路径
            path_obj = self._validate_image_path(image_path)

            # 确保 PaddleOCR 已初始化
            self._init_ocr()

            # 在线程池中执行 CPU 密集型操作
            assert self._reader is not None  # mypy: 确保 _reader 不是 None
            result = await run_in_executor(self._reader.ocr, str(path_obj), cls=True)

            if not result or not result[0]:
                logger.debug("PaddleOCR: 图片中未检测到文字")
                return None

            # 提取所有文本
            texts = [line[1][0] for line in result[0]]
            full_text = " ".join(texts)

            logger.debug(f"PaddleOCR 提取成功 [字数:{len(full_text)}]")

            return OCRResult(
                text=full_text,
                provider=self.name,
                confidence=0.95,  # PaddleOCR 不返回整体置信度
            )

        except Exception as e:
            logger.error(f"PaddleOCR 提取失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"提取失败: {e}") from e

    async def extract_with_details(self, image_path: str) -> list[tuple[str, float]] | None:
        """从图片中提取文本（带详细信息）"""
        try:
            # 验证图片路径
            path_obj = self._validate_image_path(image_path)

            # 确保 PaddleOCR 已初始化
            self._init_ocr()

            # 在线程池中执行 CPU 密集型操作
            assert self._reader is not None  # mypy: 确保 _reader 不是 None
            result = await run_in_executor(self._reader.ocr, str(path_obj), cls=True)

            if not result or not result[0]:
                return None

            # 提取文本和置信度
            texts_with_conf = [(line[1][0], line[1][1]) for line in result[0]]  # (文本, 置信度)

            return texts_with_conf if texts_with_conf else None

        except Exception as e:
            logger.error(f"PaddleOCR 提取失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"提取失败: {e}") from e


# ============================================================================
# EasyOCR 提供者（包装现有实现）
# ============================================================================


class EasyOCRProvider(OCRProvider):
    """EasyOCR 提供者（当前实现）

    特点:
    - 本地运行，内存占用 ~4-8GB
    - 兼容性好，支持虚拟化环境
    - 模型约 500MB

    环境变量:
    - OCR_EASY_ENABLED: 是否启用（默认：True）
    """

    def __init__(self, languages: list[str] | None = None):
        super().__init__("easy")
        self.languages = languages or ["ch_sim", "en"]
        self._reader: Any | None = None

    def _init_ocr(self) -> None:
        """延迟初始化 EasyOCR"""
        if self._reader is not None:
            return

        try:
            import easyocr

            logger.info("正在初始化 EasyOCR（首次使用会下载模型，约 500MB）...")

            self._reader = easyocr.Reader(
                self.languages,
                gpu=False,
                model_storage_directory=str(Path.home() / ".EasyOCR" / "model"),
                download_enabled=True,
                verbose=False,
            )

            logger.info("✅ EasyOCR 初始化成功")

        except ImportError:
            logger.warning("EasyOCR 未安装，安装命令: pip install easyocr torch torchvision")
            raise OCRProviderConfigError(self.name, "EasyOCR 未安装") from None
        except Exception as e:
            logger.error(f"EasyOCR 初始化失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"初始化失败: {e}") from e

    @property
    def is_available(self) -> bool:
        """检查 EasyOCR 是否可用"""
        try:
            self._init_ocr()
            return True
        except Exception:
            return False

    async def extract(self, image_path: str) -> OCRResult | None:
        """从图片中提取文本"""
        try:
            # 验证图片路径
            path_obj = self._validate_image_path(image_path)

            # 确保 EasyOCR 已初始化
            self._init_ocr()

            # 在线程池中执行 CPU 密集型操作
            assert self._reader is not None  # mypy: 确保 _reader 不是 None
            result = await run_in_executor(self._reader.readtext, str(path_obj))

            if not result:
                logger.debug("EasyOCR: 图片中未检测到文字")
                return None

            # 提取所有文本块
            texts = []
            for detection in result:
                # detection 格式: (bbox, text, confidence)
                _bbox, text, confidence = detection

                # 过滤低置信度结果
                if confidence > 0.6:
                    texts.append(text)

            if not texts:
                logger.debug("EasyOCR: 图片中未提取到有效文字")
                return None

            # 拼接所有文本
            full_text = " ".join(texts)

            logger.debug(f"EasyOCR 提取成功 [字数:{len(full_text)}]")

            return OCRResult(
                text=full_text,
                provider=self.name,
                confidence=0.95,  # EasyOCR 不返回整体置信度
            )

        except Exception as e:
            logger.error(f"EasyOCR 提取失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"提取失败: {e}") from e

    async def extract_with_details(self, image_path: str) -> list[tuple[str, float]] | None:
        """从图片中提取文本（带详细信息）"""
        try:
            # 验证图片路径
            path_obj = self._validate_image_path(image_path)

            # 确保 EasyOCR 已初始化
            self._init_ocr()

            # 在线程池中执行 CPU 密集型操作
            assert self._reader is not None  # mypy: 确保 _reader 不是 None
            result = await run_in_executor(self._reader.readtext, str(path_obj))

            if not result:
                return None

            # 提取文本和置信度
            texts_with_conf = []
            for detection in result:
                _bbox, text, confidence = detection
                if confidence > 0.6:
                    texts_with_conf.append((text, confidence))

            return texts_with_conf if texts_with_conf else None

        except Exception as e:
            logger.error(f"EasyOCR 提取失败: {e}")
            raise OCRProviderRuntimeError(self.name, f"提取失败: {e}") from e
