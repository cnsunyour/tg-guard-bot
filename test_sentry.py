#!/usr/bin/env python3
"""测试 Sentry 集成和敏感数据过滤"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

from src.core.config import settings

print("=" * 60)
print("Sentry 集成测试")
print("=" * 60)

# 检查配置
print(f"\n✓ Sentry DSN 配置: {'已配置' if settings.sentry_dsn else '未配置 (跳过 Sentry)'}")
print(f"✓ Sentry 环境: {settings.sentry_environment}")
print(f"✓ 采样率: {settings.sentry_traces_sample_rate}")

# 如果配置了 Sentry，测试初始化
if settings.sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.loguru import LoguruIntegration
    from src.main import before_send

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[
            LoguruIntegration(
                level=None,
                event_level="ERROR",
            )
        ],
        release="tg-guard-bot@0.1.0",
        attach_stacktrace=True,
        send_default_pii=False,
        before_send=before_send,  # 使用敏感数据过滤钩子
    )

    print("\n✓ Sentry SDK 初始化成功")
    print("✓ 已启用敏感数据过滤")
    print("✓ 已启用网络错误过滤")

    # 测试敏感数据过滤
    print("\n" + "=" * 60)
    print("测试 1: 敏感数据过滤功能")
    print("=" * 60)

    # 模拟一个包含 Bot Token 的错误
    fake_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz012345678"
    fake_url = f"https://api.telegram.org/bot{fake_token}/getUpdates"

    print(f"\n测试数据:")
    print(f"  假 Token: {fake_token}")
    print(f"  假 URL: {fake_url}")

    # 测试过滤函数
    test_event = {
        "message": f"Failed to fetch from {fake_url}",
        "exception": {
            "values": [{
                "value": f"Cannot connect to bot {fake_token}"
            }]
        }
    }

    filtered_event = before_send(test_event.copy(), None)

    print(f"\n过滤前的消息: {test_event['message']}")
    print(f"过滤后的消息: {filtered_event['message']}")
    print(f"\n过滤前的异常: {test_event['exception']['values'][0]['value']}")
    print(f"过滤后的异常: {filtered_event['exception']['values'][0]['value']}")

    if "[FILTERED_BOT_TOKEN]" in filtered_event['message']:
        print("\n✅ 敏感数据过滤测试通过！Token 已被正确过滤")
    else:
        print("\n❌ 敏感数据过滤测试失败！Token 未被过滤")

    # 测试网络错误过滤
    print("\n" + "=" * 60)
    print("测试 2: 网络错误过滤功能")
    print("=" * 60)

    # 测试各种网络错误
    network_error_types = [
        "TelegramNetworkError",
        "ClientConnectorError",
        "TimeoutError",
        "ConnectionError",
    ]

    for error_type in network_error_types:
        test_network_event = {
            "exception": {
                "values": [{
                    "type": error_type,
                    "value": f"Test {error_type}",
                    "module": "aiogram.exceptions" if "Telegram" in error_type else "aiohttp"
                }]
            },
            "message": f"Network error: {error_type}"
        }

        result = before_send(test_network_event.copy(), None)

        if result is None:
            print(f"  ✅ {error_type:<25} - 已过滤（不发送到 Sentry）")
        else:
            print(f"  ❌ {error_type:<25} - 未过滤（会发送到 Sentry）")

    # 测试非网络错误（应该保留）
    print("\n测试非网络错误（应该保留）:")
    normal_errors = [
        {"type": "ValueError", "module": "builtins"},
        {"type": "KeyError", "module": "builtins"},
        {"type": "AttributeError", "module": "builtins"},
    ]

    for error_info in normal_errors:
        test_normal_event = {
            "exception": {
                "values": [{
                    "type": error_info["type"],
                    "value": f"Test {error_info['type']}",
                    "module": error_info["module"]
                }]
            },
            "message": f"Normal error: {error_info['type']}"
        }

        result = before_send(test_normal_event.copy(), None)

        if result is not None:
            print(f"  ✅ {error_info['type']:<25} - 已保留（会发送到 Sentry）")
        else:
            print(f"  ❌ {error_info['type']:<25} - 被过滤（不会发送）")

    print("\n✅ 网络错误过滤测试完成！")

    # 测试发送一个测试事件（可选）
    print("\n" + "=" * 60)
    choice = input("是否发送测试事件到 Sentry? (y/N): ").strip().lower()
    if choice == 'y':
        print("\n发送测试事件（包含假 Token，应被过滤）...")
        try:
            # 触发一个包含 Token 的测试异常
            raise ValueError(f"Test error with token: {fake_token} and URL: {fake_url}")
        except ValueError:
            sentry_sdk.capture_exception()
            print("✓ 测试事件已发送")
            print("✓ 请在 Sentry 控制台查看，Token 应显示为 [FILTERED_BOT_TOKEN]")
    else:
        print("✓ 跳过发送测试事件")
else:
    print("\n⚠️ Sentry DSN 未配置，无法测试发送事件")
    print("如需启用 Sentry：")
    print("  1. 在 https://sentry.io 注册账号")
    print("  2. 创建项目并获取 DSN")
    print("  3. 在 .env 文件中设置 SENTRY_DSN")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
