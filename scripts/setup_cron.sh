#!/bin/bash
# 设置 Telegram Bot 自动备份定时任务
# 每日凌晨 3:00 执行备份（PostgreSQL + Redis + GFS 轮转）

set -e

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_NAME="tg-guard-bot"

echo "================================================"
echo "  Telegram Bot 自动备份定时任务配置"
echo "================================================"
echo ""
echo "项目目录: $PROJECT_DIR"
echo "备份时间: 每日凌晨 3:00"
echo "备份策略: Daily (7天) + Weekly (4周)"
echo ""

# 定义 crontab 任务
CRON_JOB="0 3 * * * cd $PROJECT_DIR && make backup >> $PROJECT_DIR/logs/backup.log 2>&1"
CRON_MARKER="# $PROJECT_NAME auto backup"

# 检查是否已存在定时任务
if crontab -l 2>/dev/null | grep -q "$PROJECT_NAME.*backup"; then
    echo -e "${YELLOW}⚠️  定时任务已存在${NC}"
    echo ""
    echo "当前配置："
    crontab -l 2>/dev/null | grep "$PROJECT_NAME"
    echo ""
    read -p "是否要重新配置？(y/N) " -n 1 -r
    echo ""

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "取消配置"
        exit 0
    fi

    # 删除旧的配置
    crontab -l 2>/dev/null | grep -v "$PROJECT_NAME" | crontab -
    echo -e "${GREEN}✅ 已删除旧的定时任务${NC}"
fi

# 添加新的定时任务
(
    crontab -l 2>/dev/null || true
    echo ""
    echo "$CRON_MARKER"
    echo "$CRON_JOB"
) | crontab -

echo -e "${GREEN}✅ 定时任务已添加！${NC}"
echo ""
echo "配置详情："
echo "-------------------------------------------"
crontab -l 2>/dev/null | tail -2
echo "-------------------------------------------"
echo ""
echo "备份日志: $PROJECT_DIR/logs/backup.log"
echo ""
echo "验证命令:"
echo "  crontab -l          # 查看所有定时任务"
echo "  make backup         # 手动执行备份测试"
echo "  make backup-list    # 查看备份列表"
echo ""
echo "删除定时任务:"
echo "  crontab -l | grep -v '$PROJECT_NAME' | crontab -"
echo ""
echo -e "${GREEN}配置完成！明天凌晨 3:00 将自动执行首次备份。${NC}"
