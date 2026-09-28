"""自定义验证脚本管理命令的 handler 测试（mock service/权限，无真实网络）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message

from src.bot.handlers import custom_verify as handler
from src.core.config import settings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CHAT = -100123
ADMIN = 42
STRANGER = 7


def _message(chat_type: str = "group", user_id: int = ADMIN) -> Message:
    message = AsyncMock(spec=Message)
    message.chat = SimpleNamespace(id=CHAT, type=chat_type)
    message.from_user = SimpleNamespace(id=user_id)
    message.sender_chat = None  # 匿名管理员识别字段（message 版权限检查访问）
    message.answer = AsyncMock()
    return message


@pytest.fixture
def admin_user(monkeypatch: pytest.MonkeyPatch):
    """调用者即超级管理员（settings.admin_ids 直通）。"""
    monkeypatch.setattr(settings, "admin_ids", {ADMIN})
    return ADMIN


@pytest.fixture
def feature_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "custom_verification_enabled", True)
    client = SimpleNamespace(configured=True)
    monkeypatch.setattr("src.services.sandbox_client.get_sandbox_client", lambda: client)


class TestGlobalSwitch:
    async def test_globally_disabled_blocks_all(self, admin_user, monkeypatch):
        monkeypatch.setattr(settings, "custom_verification_enabled", False)
        message = _message()
        localizer = SimpleNamespace(t=lambda key, **kw: key)
        await handler.cmd_customverify(
            message,
            AsyncMock(),
            localizer,
            SimpleNamespace(args="upload"),
        )
        # 全局关闭时任何子命令都提示未启用，不触达上传流程
        message.answer.assert_awaited_once_with("customverify.globally_disabled.message")

    async def test_stranger_denied_in_group(self, admin_user, monkeypatch):
        # 非管理员（admin_ids 不含 + 权限查询 False）：权限拒绝
        monkeypatch.setattr(handler, "check_admin_permission_by_id", AsyncMock(return_value=False))
        message = _message(user_id=STRANGER)
        localizer = SimpleNamespace(t=lambda key, **kw: key)
        await handler.cmd_customverify(
            message, AsyncMock(), localizer, SimpleNamespace(args="status")
        )
        message.answer.assert_awaited_once_with("customverify.permission_denied.message")


class TestUploadFlow:
    async def test_upload_writes_waiting_key(self, admin_user, feature_on, monkeypatch):
        redis = SimpleNamespace(set=AsyncMock(return_value=True), get=AsyncMock(return_value=None))
        monkeypatch.setattr(handler, "get_redis", lambda: redis)
        monkeypatch.setattr(handler, "check_admin_permission_by_id", AsyncMock(return_value=True))
        message = _message()
        localizer = SimpleNamespace(t=lambda key, **kw: key)

        await handler._handle_upload_request(message, localizer, CHAT, ADMIN)

        redis.set.assert_awaited_once()
        args = redis.set.await_args
        assert args.args[0] == f"custom_verify_upload:{ADMIN}"
        assert args.args[1] == str(CHAT)
        message.answer.assert_awaited_once()

    async def test_private_document_without_waiting_key_ignored(
        self, admin_user, feature_on, monkeypatch
    ):
        # 非上传流程的私聊文档：静默忽略（不 answer、不下载）
        redis = SimpleNamespace(getdel=AsyncMock(return_value=None))
        monkeypatch.setattr(handler, "get_redis", lambda: redis)
        message = _message(chat_type="private")
        message.document = SimpleNamespace(file_name="x.py", file_size=10)

        await handler.on_custom_verify_document(message, AsyncMock())

        message.answer.assert_not_awaited()
