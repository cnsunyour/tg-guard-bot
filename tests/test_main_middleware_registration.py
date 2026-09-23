"""main.setup_bot 的消息中间件分层注册契约。

安全闸门（白名单 / 入群短窗口 / 宵禁）必须以 outer 注册：aiogram 的 inner
middleware 只在 handler 匹配后执行，改回 inner 会让没有专用 handler 的消息类型
整体绕过这些检查（见 main.py 注释）。
"""

import pytest

from src import main
from src.bot.middlewares import (
    AutoDeleteMiddleware,
    CASCheckMiddleware,
    CurfewMiddleware,
    VerificationGuardMiddleware,
    WhitelistMiddleware,
)
from src.core.config import settings

pytestmark = pytest.mark.unit


async def test_message_security_gates_are_outer_middlewares(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cas_enabled", True)
    bot, dp = await main.setup_bot()
    try:
        outer = [type(m) for m in dp.message.outer_middleware]
        inner = [type(m) for m in dp.message.middleware]
        edited_outer = list(dp.edited_message.outer_middleware)
        edited_inner = list(dp.edited_message.middleware)
    finally:
        await bot.session.close()

    # 顺序有意义：白名单先于入群短窗口，再宵禁
    assert outer == [WhitelistMiddleware, VerificationGuardMiddleware, CurfewMiddleware]
    # 依赖 handler 语义 / 较重的检查保持 inner
    assert inner == [AutoDeleteMiddleware, CASCheckMiddleware]
    # 编辑不算「发送」：闸门只挂 message observer
    assert edited_outer == []
    assert edited_inner == []
