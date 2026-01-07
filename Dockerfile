# Python 3.12 基础镜像
FROM python:3.12-slim

# 构建参数：是否启用 OCR（默认禁用）
ARG ENABLE_OCR=false

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装基础系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libwebp7 \
    libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

# 如果启用 OCR，安装额外的系统依赖
RUN if [ "$ENABLE_OCR" = "true" ]; then \
    apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender-dev \
        libgl1 \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*; \
    fi

# 复制依赖文件
COPY pyproject.toml ./

# 安装 Python 依赖
RUN pip install --upgrade pip setuptools wheel && \
    if [ "$ENABLE_OCR" = "true" ]; then \
        pip install -e ".[ocr]"; \
    else \
        pip install -e .; \
    fi

# 复制项目代码
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/

# 创建日志和数据目录
RUN mkdir -p /app/logs /app/data/models

# ✅ L1: 创建非 root 用户运行应用（安全最佳实践）
RUN groupadd -r appuser && \
    useradd -r -g appuser -u 1000 appuser && \
    chown -R appuser:appuser /app

# 切换到非特权用户
USER appuser

# 运行 Bot
CMD ["python", "-m", "src.main"]
