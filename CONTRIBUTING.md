# 开发者指南

本文档介绍如何设置开发环境、代码规范和工作流程。

## 📋 目录

- [快速开始](#快速开始)
- [开发环境设置](#开发环境设置)
- [代码规范](#代码规范)
- [测试](#测试)
- [提交代码](#提交代码)

---

## 🚀 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/cnsunyour/tg-guard-bot.git
cd tg-guard-bot

# 2. 安装开发依赖
make install-dev

# 3. 复制环境配置
cp .env.example .env
# 编辑 .env，填入必要的配置（BOT_TOKEN, DATABASE 等）

# 4. 启动开发环境
make dev-up

# 5. 运行测试
make test
```

---

## 🛠️ 开发环境设置

### 系统要求

- Python 3.12+
- Docker & Docker Compose
- Make（可选，但推荐）

### 安装依赖

项目使用 `pyproject.toml` 统一管理所有依赖和工具配置。

```bash
# 仅安装生产依赖
make install

# 安装开发依赖（推荐）
make install-dev

# 安装所有依赖（包括 OCR）
make install-all
```

**手动安装方式**：

```bash
# 开发环境
pip install -e ".[dev]"

# 包含 OCR
pip install -e ".[all]"
```

### 🔥 热更新开发

开发环境默认启用热更新功能，修改代码后会自动重启 bot，无需手动重启。

**启动开发环境**：

```bash
# 启动（自动启用热更新）
make dev-up

# 查看日志（实时监控）
make dev-logs
```

**工作原理**：

1. ✅ `docker-compose.override.yml` 自动挂载 `src/` 目录
2. ✅ `watchfiles` 监控 Python 文件变化
3. ✅ 检测到变化后自动重启 bot 进程
4. ✅ 数据库和 Redis 端口映射到本地（调试方便）

**热更新范围**：

- ✅ 监控目录：`src/`（所有 Python 文件）
- ✅ 自动重启：修改后 1-2 秒生效
- ❌ 不监控：`data/`, `logs/`, `tests/`

**手动重启**（如需要）：

```bash
# 重启 bot 容器
make dev-restart

# 完全重启开发环境
make dev-down && make dev-up
```

**本地调试连接**：

开发环境会暴露以下端口到本地：

- 📊 PostgreSQL: `localhost:5432`（用户名：postgres，密码：见 .env）
- 🔴 Redis: `localhost:6379`（密码：见 .env）

可以使用 DBeaver、TablePlus 等工具连接调试。

---

## 📝 代码规范

项目使用统一的代码风格和质量检查工具，所有配置集中在 `pyproject.toml`。

### 代码格式化

使用 **Black** 和 **isort** 自动格式化代码。

```bash
# 格式化所有代码
make format

# 仅检查格式（不修改）
make format-check
```

**配置位置**: `[tool.black]` 和 `[tool.isort]` in `pyproject.toml`

**规则**:
- 行长度: 100 字符
- Python 版本: 3.12
- isort profile: black（确保兼容性）

### 代码检查 (Linting)

使用 **Ruff** 和 **mypy** 进行代码检查。

```bash
# 运行所有检查
make lint

# 仅 Ruff
ruff check src tests

# 仅 mypy
mypy src
```

**配置位置**: `[tool.ruff]` 和 `[tool.mypy]` in `pyproject.toml`

**启用的规则**:
- `E/W`: pycodestyle（代码风格）
- `F`: pyflakes（语法错误）
- `I`: isort（导入排序）
- `B`: flake8-bugbear（常见 bug 模式）
- `C4`: flake8-comprehensions（列表推导优化）
- `UP`: pyupgrade（现代 Python 语法）
- `ARG`: 未使用参数检查
- `SIM`: 代码简化建议
- `PTH`: pathlib 使用建议

### 类型注解

项目强制要求类型注解（`disallow_untyped_defs = true`）。

```python
# ✅ 正确
def greet(name: str) -> str:
    return f"Hello, {name}!"

# ❌ 错误（缺少类型注解）
def greet(name):
    return f"Hello, {name}!"
```

### 一键检查

运行所有检查（格式 + lint + 测试）：

```bash
make check
```

---

## 🧪 测试

### 运行测试

```bash
# 运行所有测试
make test

# 生成覆盖率报告
make test-cov

# 仅运行单元测试
make test-unit

# 仅运行集成测试
make test-integration
```

**测试文件命名**:
- `test_*.py` 或 `*_test.py`
- 测试类: `Test*`
- 测试函数: `test_*`

### 测试标记

使用 pytest markers 分类测试：

```python
import pytest

@pytest.mark.unit
def test_simple_function():
    assert 1 + 1 == 2

@pytest.mark.integration
async def test_database_connection():
    # 集成测试代码
    pass

@pytest.mark.slow
def test_ml_training():
    # 耗时测试
    pass
```

**运行特定标记的测试**:

```bash
# 跳过慢速测试
pytest -m "not slow"

# 仅运行单元测试
pytest -m unit
```

### 覆盖率要求

项目要求代码覆盖率 > 80%。

覆盖率报告会生成在：
- **终端**: 实时显示
- **HTML**: `htmlcov/index.html`
- **XML**: `coverage.xml`（用于 CI）

**配置位置**: `[tool.coverage]` in `pyproject.toml`

---

## 🔒 安全检查

### 代码安全扫描

使用 **Bandit** 检查安全漏洞：

```bash
make security
```

这会运行：
1. **Bandit**: Python 代码安全扫描
2. **Safety**: 依赖包漏洞检查

**配置位置**: `[tool.bandit]` in `pyproject.toml`

---

## 📦 提交代码

### 提交前检查清单

在提交代码前，运行：

```bash
make check
```

这会自动运行：
1. ✅ 代码格式检查（black + isort）
2. ✅ 代码质量检查（ruff + mypy）
3. ✅ 单元测试
4. ✅ 覆盖率检查

### Git Commit 规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**类型 (type)**:
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式（不影响功能）
- `refactor`: 重构
- `test`: 测试相关
- `chore`: 构建/工具链

**示例**:

```bash
git commit -m "feat(antispam): 添加图片 OCR 垃圾检测功能"
git commit -m "fix(database): 修复跨会话删除警告的问题"
git commit -m "docs: 更新部署文档"
```

---

## 🔄 CI/CD

### 本地 CI 检查

在推送代码前，可以运行完整的 CI 流水线：

```bash
make ci
```

这会运行：
1. ✅ 代码格式检查
2. ✅ 代码质量检查
3. ✅ 安全扫描
4. ✅ 完整测试套件

### GitHub Actions

项目使用 GitHub Actions 进行 CI/CD。每次推送都会自动运行：

- 代码格式和质量检查
- 测试和覆盖率
- 安全扫描
- Docker 镜像构建

**配置文件**: `.github/workflows/ci.yml`

---

## 🐳 Docker 开发

### 启动开发环境

```bash
# 启动所有服务（Bot + PostgreSQL + Redis）
make dev-up

# 查看日志
make dev-logs

# 停止服务
make dev-down
```

### 数据库操作

```bash
# 运行数据库迁移
make db-migrate

# 备份数据库
make db-backup

# 进入数据库 Shell
make db-shell
```

---

## 📚 常用命令速查

### 依赖管理
```bash
make install           # 安装生产依赖
make install-dev       # 安装开发依赖
make deps-update       # 更新所有依赖
make deps-lock         # 锁定依赖版本
```

### 代码质量
```bash
make format            # 格式化代码
make lint              # 代码检查
make check             # 完整检查
make security          # 安全扫描
make ci                # CI 流水线
```

### 测试
```bash
make test              # 运行测试
make test-cov          # 测试 + 覆盖率
make test-unit         # 单元测试
make test-integration  # 集成测试
```

### Docker
```bash
make dev-up            # 启动开发环境
make dev-down          # 停止开发环境
make dev-logs          # 查看日志
make prod-build        # 构建生产镜像
make prod-up           # 启动生产环境
```

### 数据库
```bash
make db-migrate        # 数据库迁移
make db-backup         # 备份数据库
make db-restore        # 恢复数据库
make db-shell          # 数据库 Shell
```

### 维护
```bash
make clean             # 清理临时文件
make status            # 查看服务状态
```

---

## 🔧 工具配置

所有工具配置集中在 `pyproject.toml`，无需额外配置文件。

### 配置章节

- `[project]`: 项目元数据和依赖
- `[tool.black]`: 代码格式化
- `[tool.isort]`: 导入排序
- `[tool.ruff]`: 快速 Linting
- `[tool.mypy]`: 类型检查
- `[tool.pytest.ini_options]`: 测试配置
- `[tool.coverage]`: 覆盖率配置
- `[tool.bandit]`: 安全扫描

### 自定义配置

如需修改配置，直接编辑 `pyproject.toml` 对应章节即可。

**示例 - 修改行长度**:

```toml
[tool.black]
line-length = 120  # 从 100 改为 120

[tool.ruff]
line-length = 120  # 同步修改
```

---

## 💡 最佳实践

### 代码风格

1. **遵循 PEP 8** - 使用 Black 自动格式化
2. **添加类型注解** - 所有函数都需要类型注解
3. **编写文档字符串** - 使用 Google 风格
4. **简洁明了** - 避免过度复杂的代码

### 测试

1. **测试驱动开发 (TDD)** - 先写测试，后写代码
2. **单元测试优先** - 保持测试快速
3. **集成测试补充** - 测试关键流程
4. **保持覆盖率** - 目标 > 80%

### Git 工作流

1. **小步提交** - 一次提交只做一件事
2. **清晰的消息** - 使用 Conventional Commits
3. **提交前检查** - 运行 `make check`
4. **定期同步** - 及时 pull 和 push

---

## 🆘 常见问题

### Q: 如何添加新依赖？

A: 编辑 `pyproject.toml` 的 `dependencies` 或 `[project.optional-dependencies]` 部分，然后运行：

```bash
pip install -e ".[dev]"
```

### Q: 测试失败怎么办？

A:

1. 查看详细错误信息: `pytest -v`
2. 运行单个测试: `pytest tests/test_xxx.py::test_function`
3. 查看覆盖率: `make test-cov`

### Q: 代码格式检查失败？

A:

```bash
# 自动修复格式问题
make format

# 再次检查
make format-check
```

### Q: mypy 类型检查错误？

A:

1. 添加缺失的类型注解
2. 对于第三方库，在 `pyproject.toml` 的 `[[tool.mypy.overrides]]` 中添加忽略规则

---

## 📞 获取帮助

- **文档**: 查看 `README.md` 和本指南
- **命令帮助**: `make help`
- **问题反馈**: 提交 GitHub Issue

---

**Happy Coding! 🎉**
