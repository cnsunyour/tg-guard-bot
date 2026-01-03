#!/bin/bash
# 开发环境启动脚本 - 支持热重载

set -e

echo "🔥 开发模式：启用热重载"
echo "================================"
echo "监控目录: src/"
echo "文件类型: *.py"
echo "================================"
echo ""

# 确保开发依赖已安装
if ! python -c "import watchfiles" 2>/dev/null; then
    echo "📦 安装开发依赖..."
    pip install -e ".[dev]" --quiet
    echo "✅ 依赖安装完成"
    echo ""
fi

# 使用 watchfiles 监控源代码变化并自动重启
echo "🚀 启动 Bot（监控文件变化）..."
echo "按 Ctrl+C 停止"
echo ""

exec watchfiles \
    --filter python \
    'python -m src.main' \
    src/
