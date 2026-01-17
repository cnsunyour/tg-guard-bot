#!/usr/bin/env python3
"""Telethon 首次登录脚本

用于生成 session 文件，需要交互式输入手机号和验证码
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from telethon import TelegramClient

from src.core.proxy import get_telethon_proxy


async def main():
    """主函数"""
    print("=" * 60)
    print("Telethon 登录脚本")
    print("=" * 60)
    print()

    # 获取 API 凭据
    print("请输入 Telegram API 凭据（从 https://my.telegram.org 获取）:")
    api_id_str = input("API ID: ").strip()
    api_hash = input("API Hash: ").strip()

    if not api_id_str or not api_hash:
        print("❌ API ID 和 API Hash 不能为空")
        sys.exit(1)

    try:
        api_id = int(api_id_str)
    except ValueError:
        print("❌ API ID 必须是数字")
        sys.exit(1)

    # Session 文件路径
    session_path = Path("data/user_bot.session")
    session_path.parent.mkdir(parents=True, exist_ok=True)

    # 去掉 .session 后缀
    session_name = str(session_path.with_suffix(""))

    print()
    print(f"Session 文件将保存到: {session_path}")
    print()

    # 检测代理配置
    proxy = get_telethon_proxy()
    if proxy:
        proxy_type, host, port = proxy
        print(f"✓ 检测到代理配置: {proxy_type}://{host}:{port}")
        print()

    # 创建客户端
    client = TelegramClient(session_name, api_id, api_hash, proxy=proxy)

    try:
        # 启动客户端（会交互式要求输入手机号和验证码）
        await client.start()

        # 获取当前用户信息
        me = await client.get_me()
        print()
        print("=" * 60)
        print("✅ 登录成功！")
        print("=" * 60)
        print(f"用户名: {me.first_name} {me.last_name or ''}")
        print(f"用户 ID: {me.id}")
        print(f"用户名: @{me.username or 'N/A'}")
        print(f"手机号: {me.phone or 'N/A'}")
        print()
        print(f"Session 文件已保存到: {session_path}")
        print()
        print("下一步:")
        print("1. 将 session 文件部署到服务器")
        print("2. 在 .env 中配置:")
        print(f"   TELETHON_API_ID={api_id}")
        print(f"   TELETHON_API_HASH={api_hash}")
        print(f"   TELETHON_SESSION_PATH={session_path}")
        print("   TELETHON_ENABLED=true")
        print()

    except KeyboardInterrupt:
        print("\n\n❌ 用户取消")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 登录失败: {e}")
        sys.exit(1)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
