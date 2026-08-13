# Python 3.13 基础镜像
FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖（含 cairosvg/TGS 贴纸渲染所需的 Cairo/Pango 库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libwebp7 \
    libjpeg62-turbo \
    ffmpeg \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY pyproject.toml ./

# 安装 Python 依赖
RUN pip install --upgrade pip setuptools wheel && \
    pip install -e .

# 复制项目代码
COPY src/ ./src/
COPY alembic.ini ./
COPY docker-entrypoint.sh ./
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/
COPY locales/ ./locales/
COPY config/ ./config/

# 确保容器入口脚本可执行
RUN chmod +x /app/docker-entrypoint.sh

# 创建日志、数据和缓存目录
RUN mkdir -p /app/logs /app/data/models /app/data/.cache/fastembed /app/data/.cache/huggingface

# ✅ L1: 创建非 root 用户运行应用（安全最佳实践）
RUN groupadd -r appuser && \
    useradd -r -g appuser -u 1000 -m -d /home/appuser appuser && \
    mkdir -p /home/appuser/.cache && \
    chown -R appuser:appuser /app /home/appuser

# 设置缓存路径环境变量
ENV FASTEMBED_CACHE_PATH=/app/data/.cache/fastembed \
    HF_HOME=/app/data/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/data/.cache/huggingface \
    XDG_CACHE_HOME=/app/data/.cache

# 切换到非特权用户
USER appuser

# 启动前先执行数据库迁移（见 docker-entrypoint.sh）
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# 运行 Bot
CMD ["python", "-m", "src.main"]
