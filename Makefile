.PHONY: help build up down restart logs clean backup restore migrate test

# 默认目标
help:
	@echo "Telegram Guard Bot - 管理命令"
	@echo ""
	@echo "开发环境:"
	@echo "  make dev-up          - 启动开发环境"
	@echo "  make dev-down        - 停止开发环境"
	@echo "  make dev-logs        - 查看开发环境日志"
	@echo ""
	@echo "生产环境:"
	@echo "  make prod-build      - 构建生产环境镜像"
	@echo "  make prod-build-ocr  - 构建生产环境镜像（启用 OCR）"
	@echo "  make prod-up         - 启动生产环境"
	@echo "  make prod-down       - 停止生产环境"
	@echo "  make prod-restart    - 重启生产环境"
	@echo "  make prod-logs       - 查看生产环境日志"
	@echo ""
	@echo "数据库:"
	@echo "  make db-migrate      - 运行数据库迁移"
	@echo "  make db-backup       - 备份数据库"
	@echo "  make db-restore      - 恢复数据库"
	@echo "  make db-shell        - 进入数据库 Shell"
	@echo ""
	@echo "模型训练:"
	@echo "  make train-samples   - 添加示例训练数据"
	@echo "  make train-model     - 训练反垃圾模型"
	@echo ""
	@echo "维护:"
	@echo "  make clean           - 清理临时文件"
	@echo "  make clean-all       - 清理所有数据（包括数据库）"
	@echo "  make status          - 查看服务状态"

# 开发环境
dev-up:
	docker-compose up -d
	@echo "✅ 开发环境已启动"
	@echo "查看日志: make dev-logs"

dev-down:
	docker-compose down
	@echo "✅ 开发环境已停止"

dev-logs:
	docker-compose logs -f

# 生产环境
prod-build:
	docker-compose -f docker-compose.yml -f docker-compose.prod.yml build
	@echo "✅ 生产环境镜像构建完成"

prod-build-ocr:
	docker-compose -f docker-compose.yml -f docker-compose.prod.yml build --build-arg ENABLE_OCR=true
	@echo "✅ 生产环境镜像构建完成（启用 OCR）"

prod-up:
	docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
	@echo "✅ 生产环境已启动"
	@echo "查看日志: make prod-logs"
	@echo "查看状态: make status"

prod-down:
	docker-compose -f docker-compose.yml -f docker-compose.prod.yml down
	@echo "✅ 生产环境已停止"

prod-restart:
	docker-compose -f docker-compose.yml -f docker-compose.prod.yml restart bot
	@echo "✅ Bot 已重启"

prod-logs:
	docker-compose -f docker-compose.yml -f docker-compose.prod.yml logs -f bot

# 数据库操作
db-migrate:
	docker-compose exec bot python scripts/migrate.py
	@echo "✅ 数据库迁移完成"

db-backup:
	@mkdir -p backups
	docker-compose exec -T postgres pg_dump -U postgres tg_guard > backups/backup_$$(date +%Y%m%d_%H%M%S).sql
	@echo "✅ 数据库已备份到 backups/"

db-restore:
	@read -p "输入备份文件名 (backups/backup_YYYYMMDD_HHMMSS.sql): " backup_file; \
	docker-compose exec -T postgres psql -U postgres tg_guard < $$backup_file
	@echo "✅ 数据库已恢复"

db-shell:
	docker-compose exec postgres psql -U postgres -d tg_guard

# 模型训练
train-samples:
	docker-compose exec bot python scripts/train_model.py --add-samples
	@echo "✅ 示例数据已添加"

train-model:
	docker-compose exec bot python scripts/train_model.py --train
	@echo "✅ 模型训练完成"

# 维护
clean:
	@echo "清理临时文件..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".DS_Store" -delete
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	@echo "✅ 清理完成"

clean-all: clean
	@read -p "⚠️  确定要删除所有数据（包括数据库）吗？(yes/no): " confirm; \
	if [ "$$confirm" = "yes" ]; then \
		docker-compose down -v; \
		rm -rf data/models/*; \
		rm -rf logs/*; \
		echo "✅ 所有数据已清理"; \
	else \
		echo "❌ 取消操作"; \
	fi

status:
	@echo "=== Docker 容器状态 ==="
	docker-compose ps
	@echo ""
	@echo "=== Docker 资源使用 ==="
	docker stats --no-stream tg-guard-bot tg-guard-postgres tg-guard-redis || true
