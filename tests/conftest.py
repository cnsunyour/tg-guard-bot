"""Pytest 配置和共享 fixtures"""

import os
import shutil
import socket
import subprocess
import time

import pytest
import redis as sync_redis

# 必填配置兜底：无 .env 的环境（如 CI）也能完成测试收集与运行。
# 必须在模块顶层执行（收集阶段先于任何 fixture），setdefault 不覆盖显式配置。
# 四项与下方 mock_settings fixture 的 setenv 集合对齐（含 model_post_init 生产校验）
os.environ.setdefault("BOT_TOKEN", "123456:TEST_BOT_TOKEN")
os.environ.setdefault("MODEL_SIGNATURE_KEY", "t" * 64)
os.environ.setdefault("DB_PASSWORD", "ci-test-db-password")
os.environ.setdefault("REDIS_PASSWORD", "ci-test-redis-password")


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """设置测试环境变量"""
    # 设置测试环境标志
    os.environ["TESTING"] = "true"
    os.environ["LOG_LEVEL"] = "DEBUG"


def _redis_ping(url: str) -> bool:
    """探测 Redis 是否可达（同步客户端，供 fixture 启动前使用）"""
    try:
        client = sync_redis.Redis.from_url(url, socket_timeout=0.5)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


def _find_redis_server() -> str | None:
    """定位 redis-server 可执行文件（PATH 优先，再查 Homebrew 前缀）"""
    found = shutil.which("redis-server")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/opt/redis/bin/redis-server",  # macOS Apple Silicon
        "/usr/local/opt/redis/bin/redis-server",  # macOS Intel
        "/usr/bin/redis-server",  # Linux 常见路径
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _free_port() -> int:
    """取一个空闲本地端口（存在 close 后被抢占的微小竞态，启动失败时兜底跳过）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def real_redis_url():
    """返回可用的真实 Redis URL，供集成测试使用

    探测顺序：
    1. 显式设置 ``REDIS_TEST_URL``：尊重用户指定，不可达即跳过（不代起实例）
    2. 默认 ``redis://localhost:6379/15`` 可达：直接复用（本机服务或 CI 容器）
    3. 本机装有 redis-server 但未启动：拉起**临时实例**（独立空闲端口、
       无持久化、仅监听 127.0.0.1），会话结束自动终止——不触碰 6379 上的
       本机服务配置与数据，停掉的服务保持停掉
    4. 未安装 redis-server：跳过
    """
    explicit = os.getenv("REDIS_TEST_URL")
    if explicit:
        if _redis_ping(explicit):
            yield explicit
        else:
            pytest.skip(f"REDIS_TEST_URL 显式指定但不可达（{explicit}）")
        return

    default_url = "redis://localhost:6379/15"
    if _redis_ping(default_url):
        yield default_url
        return

    server = _find_redis_server()
    if server is None:
        pytest.skip("本机未安装 redis-server（REDIS_TEST_URL 不可达），跳过真实 Redis 集成测试")

    port = _free_port()
    url = f"redis://localhost:{port}/15"
    try:
        proc = subprocess.Popen(  # noqa: RUF006（进程引用由本 fixture 持有到会话结束）
            [
                server,
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        pytest.skip(f"临时 redis-server 启动失败（{server}: {e}）")

    try:
        # 轮询等待实例就绪（本地启动通常 <100ms，上限 5s）。就绪判定必须是
        # 「PING 通 且 本进程仍存活」：端口若被抢占（如并行测试会话拉起的实例），
        # 本进程会因 bind 失败退出，PING 通的只是别人的实例——此时按启动失败跳过，
        # 绝不误用可能被并发污染的实例
        deadline = time.monotonic() + 5.0
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # 本进程已退出（端口被抢占等）
            if _redis_ping(url) and proc.poll() is None:
                ready = True
                break
            time.sleep(0.1)
        if not ready:
            pytest.skip(f"临时 redis-server 启动失败（端口 {port}）")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def mock_bot_token():
    """模拟 Bot Token"""
    return "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_test_token"


@pytest.fixture
def mock_admin_id():
    """模拟管理员 ID"""
    return 123456789


@pytest.fixture
def mock_settings(monkeypatch, mock_bot_token, mock_admin_id):
    """模拟完整配置"""
    monkeypatch.setenv("BOT_TOKEN", mock_bot_token)
    monkeypatch.setenv("ADMIN_IDS", str(mock_admin_id))
    monkeypatch.setenv("DB_PASSWORD", "test_password_12345")
    monkeypatch.setenv("REDIS_PASSWORD", "redis_test_password_12345")
    monkeypatch.setenv("MODEL_SIGNATURE_KEY", "a" * 64)

    from src.core.config import Settings

    return Settings()


@pytest.fixture
def sample_spam_texts():
    """垃圾消息样本"""
    return [
        "点击链接免费领取iPhone",
        "加微信：wx123456 办理贷款",
        "访问 http://bit.ly/promo 了解更多",
        "🎁免费送礼物🎁加QQ群：123456789",
        "Telegram: @spam_bot 推荐股票",
    ]


@pytest.fixture
def sample_normal_texts():
    """正常消息样本"""
    return [
        "大家好，我是新来的",
        "今天天气不错，适合出门",
        "有人知道怎么学Python吗？",
        "谢谢大家的帮助",
        "周末有什么计划？",
    ]
