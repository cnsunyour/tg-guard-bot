# Telegram API 速率限制处理

## 概述

本项目使用 **Client Session Middleware** 方案透明处理 Telegram Bot API 的速率限制（429 错误）。

## 实现细节

### 1. 中间件位置

- **文件**: `src/bot/middlewares/retry_after.py`
- **类名**: `RetryAfterMiddleware`
- **层级**: Client Session 层（在实际 HTTP 请求之前）

### 2. 工作原理

```
用户请求 → Dispatcher → Handler → Bot API 调用
                                        ↓
                            RetryAfterMiddleware (拦截)
                                        ↓
                            捕获 TelegramRetryAfter 异常
                                        ↓
                            等待 retry_after 秒
                                        ↓
                            自动重试（最多 3 次）
                                        ↓
                            返回成功响应或抛出异常
```

### 3. 关键特性

- ✅ **透明处理**: 业务代码无需修改，自动捕获和重试
- ✅ **尊重 API 限制**: 使用 Telegram 返回的精确等待时间
- ✅ **可配置重试次数**: 默认最多重试 3 次，避免无限循环
- ✅ **详细日志**: 记录每次重试的详细信息，便于监控

### 4. 注册位置

在 `src/main.py` 的 `setup_bot()` 函数中：

```python
# ✅ 注册 Session 层中间件：处理 Telegram API 速率限制 (429)
from src.bot.middlewares import RetryAfterMiddleware

bot.session.middleware(RetryAfterMiddleware(max_retries=3))
```

**重要**: 必须在创建 Bot 对象后立即注册，在 Dispatcher 初始化之前。

## Telegram API 速率限制规则

| 场景 | 限制 |
|------|------|
| 同一群组发消息 | 20 条/分钟 |
| 不同聊天发消息 | 30 条/秒 |
| 群组管理操作 | 20 次/分钟 |
| GetUpdates（长轮询） | 无限制（aiogram 自动处理） |

参考：[Telegram Bot API FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)

## 日志示例

### 正常重试日志

```
⏳ Telegram API 速率限制触发，自动重试中 [方法:SendMessage] [第1次重试] [等待时间:5秒]
🔄 重试 Telegram API 请求 [方法:SendMessage] [第1次重试]
```

### 达到最大重试次数

```
❌ Telegram API 速率限制重试失败，已达到最大重试次数 [方法:SendMessage] [重试次数:3] [等待时间:10秒]
```

## 与应用层速率限制的区别

| 对比项 | 应用层限制 (ThrottleMiddleware) | API 层限制 (RetryAfterMiddleware) |
|--------|--------------------------------|-----------------------------------|
| 层级 | Dispatcher 中间件 | Client Session 中间件 |
| 目的 | 防止用户滥用命令 | 处理 Telegram API 限制 |
| 触发条件 | 用户请求频率过高 | Telegram 返回 429 错误 |
| 处理方式 | **丢弃请求**，返回警告 | **排队重试**，自动等待 |
| 配置 | 3 次/秒（消息）<br>5 次/秒（回调） | 遵循 API 返回的 retry_after |

## 常见场景

### 场景 1：批量验证失败

**问题**: 50 人同时入群但未启动 bot，触发 50 条引导消息

**解决**:
1. 应用层：共享引导消息机制（30 秒内只发 1 条）
2. API 层：如果仍触发限制，RetryAfterMiddleware 自动重试

### 场景 2：大规模禁言操作

**问题**: 管理员一次性禁言 100 个用户（20 次/分钟限制）

**解决**:
- RetryAfterMiddleware 自动排队重试，确保所有操作最终完成
- 日志记录每次重试，便于监控进度

## 测试

### 手动测试

1. 启动 bot：`make dev-up`
2. 在群组中触发大量消息（如批量添加用户）
3. 观察日志：`make dev-logs | grep "速率限制"`

### 预期结果

- ✅ 不会因为 429 错误而失败
- ✅ 日志显示自动重试过程
- ✅ 所有消息最终成功发送

## 监控指标

建议监控以下日志关键词：

- `⏳ Telegram API 速率限制触发` - 触发频率
- `❌ Telegram API 速率限制重试失败` - 失败次数（需警惕）

## 未来优化

如果频繁触发速率限制，可以考虑：

1. **预防式限速**: 在应用层主动控制请求速率（如每秒最多 10 个请求）
2. **消息队列**: 使用 Redis 队列实现更精细的速率控制
3. **批量操作优化**: 合并多个操作为单次 API 调用（如 `banChatMember` 批量版本）

## 参考资料

- [aiogram 3.x Client Session Middlewares](https://docs.aiogram.dev/en/latest/api/session/middleware.html)
- [Strategy to deal with TelegramRetryAfter errors](https://github.com/aiogram/aiogram/discussions/1489)
- [Telegram Bot API Rate Limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)

---

**最后更新**: 2026-01-08
**版本**: v1.0
