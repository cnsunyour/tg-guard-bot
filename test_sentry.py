#!/usr/bin/env python3
"""测试 Sentry 集成"""

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
    )

    print("\n✓ Sentry SDK 初始化成功")

    # 测试发送一个测试事件（可选）
    choice = input("\n是否发送测试事件到 Sentry? (y/N): ").strip().lower()
    if choice == 'y':
        print("\n发送测试事件...")
        try:
            # 触发一个测试异常
            1 / 0
        except ZeroDivisionError:
            sentry_sdk.capture_exception()
            print("✓ 测试事件已发送，请在 Sentry 控制台查看")
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
