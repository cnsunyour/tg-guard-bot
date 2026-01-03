# Phase 3: 群管理功能测试指南

本指南帮助你快速测试群管理功能。

## ✅ 已实现的命令

| 命令 | 说明 | 用法示例 |
|------|------|---------|
| `/kick` | 踢出用户 | `/kick` (回复消息)<br>`/kick 123456789 违规行为` |
| `/mute` | 禁言用户 | `/mute` (回复消息)<br>`/mute 123456789 30m`<br>`/mute 123456789 2h 广告` |
| `/unmute` | 解除禁言 | `/unmute` (回复消息)<br>`/unmute 123456789` |
| `/ban` | 永久封禁 | `/ban` (回复消息)<br>`/ban 123456789 严重违规` |
| `/unban` | 解除封禁 | `/unban` (回复消息)<br>`/unban 123456789` |
| `/warn` | 警告用户 | `/warn` (回复消息)<br>`/warn 123456789 不当言论` |
| `/warnings` | 查看警告 | `/warnings` (查看自己)<br>`/warnings` (回复消息) |
| `/clearwarnings` | 清除警告 | `/clearwarnings` (回复消息) |

## 🧪 测试步骤

### 1. 准备测试环境

确保 Bot 已启动并在群组中：
```bash
docker-compose up -d
docker-compose logs -f bot
```

### 2. 测试踢人功能

1. 在群组中发送一条测试消息（用测试账号）
2. 管理员回复该消息：`/kick`
3. ✅ 预期结果：用户被踢出群组

### 3. 测试禁言功能

#### 3.1 临时禁言（30分钟）
```
回复消息: /mute 30m
```
✅ 预期结果：
- Bot 回复 "✅ 已禁言用户 xxx，时长: 30分钟"
- 用户无法发送消息
- 30分钟后自动解除

#### 3.2 永久禁言
```
回复消息: /mute
```
✅ 预期结果：
- Bot 回复 "✅ 已禁言用户 xxx，时长: 永久"
- 用户无法发送消息

#### 3.3 解除禁言
```
回复消息: /unmute
```
✅ 预期结果：
- Bot 回复 "✅ 已解除用户 xxx 的禁言"
- 用户恢复发言权限

### 4. 测试警告系统

#### 4.1 第一次警告
```
回复消息: /warn 违规行为
```
✅ 预期结果：
- Bot 回复 "⚠️ 已警告用户 xxx\n累计警告: 1/3"

#### 4.2 第二次警告
```
回复消息: /warn 再次违规
```
✅ 预期结果：
- Bot 回复 "⚠️ 已警告用户 xxx\n累计警告: 2/3"

#### 4.3 第三次警告（触发自动禁言）
```
回复消息: /warn 第三次违规
```
✅ 预期结果：
- Bot 回复 "⚠️ 已警告用户 xxx\n累计警告: 3/3\n\n🔇 用户已达到 3 次警告，自动禁言 24 小时"
- 用户被自动禁言 24 小时

#### 4.4 查看警告记录
```
回复消息: /warnings
```
✅ 预期结果：
- 显示用户的所有警告记录（最多10条）
- 包含时间、原因

#### 4.5 清除警告
```
回复消息: /clearwarnings
```
✅ 预期结果：
- Bot 回复 "✅ 已清除用户 xxx 的 3 条警告记录"
- 再次查看 `/warnings` 显示无记录

### 5. 测试封禁功能

#### 5.1 永久封禁
```
回复消息: /ban 严重违规
```
✅ 预期结果：
- Bot 回复 "✅ 已封禁用户 xxx"
- 用户被踢出群组且无法再次加入

#### 5.2 解除封禁
```
/unban 123456789
```
✅ 预期结果：
- Bot 回复 "✅ 已解除用户 xxx 的封禁"
- 用户可以再次加入群组

### 6. 测试时长格式

禁言命令支持多种时长格式：

| 格式 | 说明 | 示例 |
|------|------|------|
| `30m` | 30分钟 | `/mute 30m` |
| `2h` | 2小时 | `/mute 2h` |
| `1d` | 1天 | `/mute 1d` |
| 不填 | 永久 | `/mute` |
| `0` | 永久 | `/mute 0` |

## 📊 验证数据库

### 查看警告记录
```bash
docker-compose exec postgres psql -U postgres -d tg_guard -c "SELECT * FROM warnings ORDER BY created_at DESC LIMIT 10;"
```

### 查看审计日志
```bash
docker-compose exec postgres psql -U postgres -d tg_guard -c "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 10;"
```

### 查看群组配置
```bash
docker-compose exec postgres psql -U postgres -d tg_guard -c "SELECT * FROM groups;"
```

## 🔍 日志查看

### 查看操作日志
```bash
docker-compose logs -f bot | grep "管理员"
```

### 查看警告日志
```bash
docker-compose logs -f bot | grep "警告"
```

### 查看禁言日志
```bash
docker-compose logs -f bot | grep "禁言"
```

## ⚠️ 注意事项

### 权限要求

Bot 必须具备以下管理员权限：
- ✅ 删除消息
- ✅ 禁止用户
- ✅ 限制成员

### 测试建议

1. **使用测试账号**：不要用真实用户测试
2. **先测试解除命令**：确保可以恢复操作
3. **检查日志**：每次操作后查看日志确认执行成功
4. **验证数据库**：确保数据正确写入

### 常见问题

**Q: 命令没有响应？**
- 检查 Bot 是否是管理员
- 检查权限是否完整
- 查看日志：`docker-compose logs bot`

**Q: 禁言后用户仍能发消息？**
- 确认 Bot 有"限制成员"权限
- 重新设置 Bot 为管理员

**Q: 警告累计不触发自动禁言？**
- 检查配置：`MAX_WARNINGS=3`
- 查看数据库：确认警告记录已保存

## 🎯 下一步

Phase 3 完成后，接下来是：
- Phase 4: 反垃圾系统
- Phase 5: 图片 OCR
- Phase 6: 部署与优化
