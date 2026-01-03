# Phase 5: 图片 OCR 测试指南

本文档说明如何测试图片 OCR 反垃圾功能。

---

## 📋 前置要求

### 系统要求
- **内存**: 至少 **4GB RAM** (OCR 模型需要)
- **存储**: 额外 500MB 用于 PaddleOCR 模型

### 软件要求
- Docker Compose（推荐）或本地 Python 3.12 环境
- Telegram Bot Token
- 测试群组（建议创建专用测试群）

---

## 🚀 部署步骤

### 方式 1: Docker Compose（推荐）

#### 1. 启用 OCR 功能

在 `.env` 文件中设置：

```bash
ENABLE_OCR=true
```

#### 2. 构建并启动服务

```bash
# 重新构建镜像（启用 OCR）
docker-compose build --build-arg ENABLE_OCR=true

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f bot
```

#### 3. 验证 OCR 初始化

查看日志中是否出现：

```
PaddleOCR 初始化成功
```

---

### 方式 2: 本地开发环境

#### 1. 安装 OCR 依赖

```bash
# 安装 OCR 可选依赖
pip install -e ".[ocr]"
```

#### 2. 启动服务

```bash
# 启动数据库和 Redis
docker-compose up -d postgres redis

# 启动 Bot
python -m src.main
```

---

## 🧪 测试场景

### 测试 1: 基础 OCR 文字提取

**目的**: 验证 OCR 能否正确提取图片中的文字

**步骤**:
1. 在测试群组发送一张包含清晰中文文字的图片
2. 观察 Bot 日志

**预期结果**:
```
从图片提取文字 [用户:123456] 长度: 50 内容: 这是测试文字...
```

---

### 测试 2: 检测图片垃圾广告（关键词触发）

**目的**: 验证图片中的垃圾关键词能被检测

**步骤**:
1. 创建一张图片，包含垃圾关键词（如："加微信免费领取"）
2. 在测试群组发送该图片
3. 观察 Bot 行为

**预期结果**:
- ✅ 图片消息被删除
- ✅ 用户被禁言 10 分钟
- ✅ 收到提示消息：
  ```
  🚫 检测到图片垃圾信息并已处理

  用户: 测试用户
  原因: 图片 OCR, 关键词: 加微信
  置信度: 95.00%
  处罚: 禁言 10 分钟
  OCR 提取: 加微信免费领取...
  ```

---

### 测试 3: 检测图片广告（链接触发）

**目的**: 验证图片中的链接能被检测

**步骤**:
1. 创建一张图片，包含 Telegram 邀请链接（如："t.me/+abc123"）
2. 发送到测试群组

**预期结果**:
- ✅ 图片被删除
- ✅ 用户被禁言
- ✅ 原因显示: "图片 OCR, TG 邀请链接"

---

### 测试 4: 检测图片广告（ML 分类器触发）

**目的**: 验证 OCR 提取的文字能通过 ML 分类器检测

**前置条件**: 已训练 ML 模型

**步骤**:
1. 创建一张图片，包含变体垃圾广告文字（如："兼  职  刷  单  日  赚  500"）
2. 发送到测试群组

**预期结果**:
- ✅ 图片被删除
- ✅ 原因显示: "图片 OCR, ML 分类器 (置信度: 0.85)"

---

### 测试 5: 正常图片不被误判

**目的**: 验证正常图片不会被误判为垃圾

**步骤**:
1. 发送包含普通文字的图片（如："今天天气不错"）
2. 发送纯风景图片（无文字）

**预期结果**:
- ✅ 消息不被删除
- ✅ 用户不被禁言
- ✅ 日志显示: "消息通过检测"

---

### 测试 6: 管理员反馈功能

**目的**: 验证管理员可以纠正误判

**步骤**:
1. Bot 处理一条图片垃圾消息
2. 管理员点击提示消息下方的 "✅ 误判" 或 "❌ 确认垃圾" 按钮

**预期结果**:
- ✅ 反馈被记录到数据库
- ✅ 提示消息更新为: "✅ 确认为正常消息 (by 管理员名)"
- ✅ 日志显示: "管理员反馈 [管理员:123456] 类型: 正常"

---

## 📊 性能监控

### 1. 查看 OCR 处理时间

在日志中搜索：

```bash
docker-compose logs bot | grep "图片检测"
```

**正常范围**:
- 小图片 (<500KB): 1-3 秒
- 大图片 (1-2MB): 3-6 秒

### 2. 监控内存使用

```bash
# 查看容器内存使用
docker stats tg-guard-bot
```

**正常范围**:
- 基础运行: ~200MB
- OCR 处理时: ~1-2GB
- 峰值: <3GB

### 3. 检查 OCR 成功率

```sql
-- 连接到数据库
docker exec -it tg-guard-postgres psql -U postgres -d tg_guard

-- 查询 OCR 检测统计
SELECT
    COUNT(*) as total,
    SUM(CASE WHEN details->>'ocr_text' IS NOT NULL THEN 1 ELSE 0 END) as with_ocr
FROM audit_logs
WHERE action = 'mute'
  AND created_at > NOW() - INTERVAL '24 hours';
```

---

## ⚠️ 常见问题

### Q1: OCR 初始化失败

**症状**: 日志显示 "PaddleOCR 未安装"

**解决**:
```bash
# Docker 方式：重新构建
docker-compose build --build-arg ENABLE_OCR=true

# 本地方式：安装依赖
pip install -e ".[ocr]"
```

---

### Q2: OCR 检测速度慢

**症状**: 图片处理时间超过 10 秒

**可能原因**:
- VPS 内存不足（交换空间占用）
- CPU 性能较弱
- 图片尺寸过大

**优化建议**:
1. 升级 VPS 到 4GB RAM
2. 在 `src/ml/ocr.py` 中添加图片缩放：
   ```python
   # 在 extract_text 方法中添加
   from PIL import Image
   img = Image.open(image_path)
   if img.width > 2000 or img.height > 2000:
       img.thumbnail((2000, 2000))
       img.save(image_path)
   ```

---

### Q3: OCR 识别率低

**症状**: 图片中有文字但未提取到

**可能原因**:
- 文字太小或模糊
- 字体特殊（艺术字、手写体）
- 图片背景复杂

**解决**:
- 在 `src/ml/ocr.py` 中降低置信度阈值：
  ```python
  if confidence > 0.5:  # 原来是 0.6
      texts.append(text)
  ```

---

### Q4: 图片消息未被检测

**症状**: 发送包含垃圾的图片但未被处理

**检查清单**:
1. 确认群组已启用反垃圾: `/antispam`
2. 确认发送者不是管理员
3. 确认 OCR 已初始化: `docker-compose logs bot | grep PaddleOCR`
4. 查看详细日志: `LOG_LEVEL=DEBUG`

---

## 🔧 配置调优

### 调整 OCR 检测阈值

在 `src/ml/ocr.py` 中修改：

```python
# 置信度阈值（建议范围: 0.5-0.8）
if confidence > 0.6:  # 降低此值可提高召回率，但会增加误判
    texts.append(text)
```

### 调整图片处理优先级

在 `src/bot/handlers/antispam.py` 中：

```python
# 添加图片尺寸限制（跳过过小的图片）
photo = message.photo[-1]
if photo.file_size < 20000:  # 小于 20KB 的图片
    return
```

---

## 📈 性能基准

| 指标 | 无 OCR | 有 OCR |
|------|--------|--------|
| 镜像大小 | ~300MB | ~1.2GB |
| 启动时间 | ~5s | ~15s |
| 内存占用（空闲） | ~200MB | ~400MB |
| 内存占用（处理中） | ~200MB | ~1.5GB |
| 文本消息处理 | <100ms | <100ms |
| 图片消息处理 | N/A | 1-5s |

---

## ✅ 验收标准

Phase 5 完成的标志：

- [ ] OCR 能成功提取图片中的中文文字
- [ ] 包含垃圾关键词的图片能被正确检测和处理
- [ ] 包含链接的图片能被检测
- [ ] 正常图片不会被误判
- [ ] 管理员反馈功能正常工作
- [ ] 图片处理时间在可接受范围（<5秒）
- [ ] 内存使用在合理范围（<3GB）
- [ ] 临时文件被正确清理

---

## 🎯 下一步

Phase 5 完成后，可以进入 **Phase 6: 部署与优化**，包括：
- 生产环境部署配置
- 日志与监控优化
- 性能调优
- 完善文档

或者，如果预算限制不允许 4GB RAM VPS，可以选择：
- 禁用 OCR（`ENABLE_OCR=false`）
- 仅依赖文本检测（Stage 1-3 管道）
- 未来需要时再启用
