.PHONY: help build up down restart logs clean backup restore migrate test lint format check security security-strict security-report install

# 默认目标
help:
	@echo "Telegram Guard Bot - 管理命令"
	@echo ""
	@echo "📦 安装和依赖:"
	@echo "  make install         - 安装生产依赖"
	@echo "  make install-dev     - 安装开发依赖"
	@echo "  make install-all     - 安装所有依赖（包括 OCR）"
	@echo ""
	@echo "🧪 测试:"
	@echo "  make test            - 运行所有测试"
	@echo "  make test-cov        - 运行测试并生成覆盖率报告"
	@echo "  make test-unit       - 仅运行单元测试"
	@echo "  make test-integration - 仅运行集成测试"
	@echo ""
	@echo "✨ 代码质量:"
	@echo "  make lint            - 运行代码检查（ruff + mypy）"
	@echo "  make format          - 格式化代码（black + isort）"
	@echo "  make format-check    - 检查代码格式"
	@echo "  make check           - 运行所有检查（格式化 + lint + 测试）"
	@echo ""
	@echo "🔒 安全扫描:"
	@echo "  make security        - 运行安全扫描（bandit + safety + pip-audit）"
	@echo "  make security-strict - 严格安全扫描（失败则退出）"
	@echo "  make security-report - 生成安全扫描报告到 reports/"
	@echo ""
	@echo "🐳 开发环境（支持热更新）:"
	@echo "  make dev-up          - 启动开发环境（自动监控文件变化）"
	@echo "  make dev-down        - 停止开发环境"
	@echo "  make dev-logs        - 查看开发环境日志"
	@echo "  make dev-restart     - 重启开发环境"
	@echo ""
	@echo "🚀 生产环境:"
	@echo "  make prod-build      - 构建生产环境镜像"
	@echo "  make prod-build-ocr  - 构建生产环境镜像（启用 OCR）"
	@echo "  make prod-up         - 启动生产环境"
	@echo "  make prod-down       - 停止生产环境"
	@echo "  make prod-restart    - 重启生产环境"
	@echo "  make prod-logs       - 查看生产环境日志"
	@echo ""
	@echo "🗄️  数据库:"
	@echo "  make db-migrate      - 运行数据库迁移"
	@echo "  make db-backup       - 备份数据库"
	@echo "  make db-restore      - 恢复数据库"
	@echo "  make db-shell        - 进入数据库 Shell"
	@echo ""
	@echo "🤖 模型训练:"
	@echo "  make train-samples   - 添加示例训练数据"
	@echo "  make train-model     - 训练反垃圾模型"
	@echo ""
	@echo "🧹 维护:"
	@echo "  make clean           - 清理临时文件"
	@echo "  make clean-all       - 清理所有数据（包括数据库）"
	@echo "  make status          - 查看服务状态"

# ============================================================================
# 安装和依赖管理
# ============================================================================
install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

install-all:
	pip install -e ".[all]"

deps-update:
	pip install --upgrade pip setuptools wheel
	pip install --upgrade -e ".[all]"

# ============================================================================
# 测试
# ============================================================================
test:
	pytest

test-cov:
	pytest --cov --cov-report=html --cov-report=term

test-unit:
	pytest -m unit

test-integration:
	pytest -m integration

test-watch:
	pytest-watch

# ============================================================================
# 代码质量
# ============================================================================
lint:
	@echo "🔍 运行 Ruff..."
	ruff check src tests
	@echo "🔍 运行 mypy..."
	-mypy src || echo "⚠️  Mypy found type errors (non-blocking)"

format:
	@echo "✨ 运行 isort..."
	isort src tests
	@echo "✨ 运行 Black..."
	black src tests

format-check:
	@echo "🔍 检查 isort..."
	isort --check-only src tests
	@echo "🔍 检查 Black..."
	black --check src tests

check: format-check lint test
	@echo "✅ 所有检查通过"

# ============================================================================
# 安全扫描
# ============================================================================
security:
	@echo "🔒 运行 Bandit 安全扫描..."
	-bandit -c pyproject.toml -r src || echo "⚠️  Bandit found security issues (non-blocking)"
	@echo ""
	@echo "🔒 运行 Safety 依赖检查..."
	-safety scan || echo "⚠️  Safety found dependency issues (non-blocking)"
	@echo ""
	@echo "🔒 运行 pip-audit 依赖扫描..."
	-pip-audit --desc || echo "⚠️  pip-audit found dependency issues (non-blocking)"

security-strict:
	@echo "🔒 运行严格安全扫描（失败则退出）..."
	@echo "🔍 Bandit..."
	bandit -c pyproject.toml -r src
	@echo "🔍 Safety..."
	safety scan
	@echo "🔍 pip-audit..."
	pip-audit
	@echo "✅ 所有安全扫描通过"

security-report:
	@echo "📊 生成安全扫描报告..."
	@mkdir -p reports
	@echo "🔍 Bandit..."
	-bandit -c pyproject.toml -r src -f json -o reports/bandit-report.json
	@echo "🔍 Safety..."
	-safety scan --output json > reports/safety-report.json
	@echo "🔍 pip-audit..."
	-pip-audit --format json --output reports/pip-audit-report.json
	@echo "✅ 报告已生成到 reports/ 目录"

# ============================================================================
# CI/CD
# ============================================================================
ci: format-check lint security test
	@echo "✅ CI 检查通过"

# 开发环境（支持热更新）
dev-up:
	docker-compose up -d
	@echo "✅ 开发环境已启动（支持热更新）"
	@echo "📝 修改 src/ 目录下的代码会自动重启 bot"
	@echo "🔍 查看日志: make dev-logs"
	@echo "🔗 数据库: localhost:5432 (postgres/postgres)"
	@echo "🔗 Redis:   localhost:6379"

dev-down:
	docker-compose down
	@echo "✅ 开发环境已停止"

dev-restart:
	docker-compose restart bot
	@echo "✅ Bot 已重启"

dev-logs:
	docker-compose logs -f bot

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

db-shell:
	docker-compose exec postgres psql -U postgres -d tg_guard

# 自动备份（PostgreSQL + Redis + GFS 轮转）
backup:
	python scripts/backup.py
	@echo "✅ 自动备份完成（PostgreSQL + Redis）"

backup-postgres:
	python scripts/backup.py --postgres
	@echo "✅ PostgreSQL 备份完成"

backup-redis:
	python scripts/backup.py --redis
	@echo "✅ Redis 备份完成"

backup-list:
	python scripts/backup.py --list

backup-cleanup:
	python scripts/backup.py --cleanup
	@echo "✅ 过期备份已清理"

backup-restore-postgres:
	@if [ -z "$(FILE)" ]; then \
		echo "错误: 请指定备份文件，例如: make backup-restore-postgres FILE=backups/daily/postgres_20260109.sql"; \
		exit 1; \
	fi
	python scripts/backup.py --restore-postgres $(FILE)
	@echo "✅ PostgreSQL 已恢复"

backup-restore-redis:
	@if [ -z "$(FILE)" ]; then \
		echo "错误: 请指定备份文件，例如: make backup-restore-redis FILE=backups/daily/redis_20260109.rdb"; \
		exit 1; \
	fi
	@echo "⚠️  警告: Redis 恢复需要重启容器"
	docker-compose stop redis
	python scripts/backup.py --restore-redis $(FILE)
	docker-compose start redis
	@echo "✅ Redis 已恢复并重启"

backup-setup-cron:
	@bash scripts/setup_cron.sh

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
