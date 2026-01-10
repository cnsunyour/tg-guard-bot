# Telegram Guard Bot - Logo 设计

> AI-Powered Group Protection Bot

## 📐 设计理念

本项目 Logo 设计融合了以下核心元素：

- **🛡️ 盾牌**：象征安全防护和群组保护
- **✅ 验证标记**：代表入群验证和内容审核
- **🤖 AI/机器人**：体现智能检测和自动化管理
- **💙 Telegram 蓝**：品牌识别和平台归属感
- **🔄 渐变色**：现代感与科技感

---

## 🎨 Logo 方案

### 方案 1: 盾牌 + 机器人 (Shield Bot)

**文件**: `logo-shield-bot.svg`

**特点**:
- 可爱的机器人形象，亲和力强
- 盾牌底纹，安全感十足
- 右下角验证勾号，功能明确
- 适合作为 Bot 头像和主 Logo

**使用场景**: Telegram Bot 头像、应用图标、品牌宣传

---

### 方案 2: 现代徽章 (Modern Badge)

**文件**: `logo-modern-badge.svg`

**特点**:
- 蓝绿渐变，视觉冲击力强
- 中心 "G" 字母 (Guard) 识别度高
- 顶部 AI 标识，突出技术特色
- 专业现代，适合商业场景

**使用场景**: 网站首页、产品宣传、社交媒体封面

---

### 方案 3: 极简扁平 (Minimal Flat)

**文件**: `logo-minimal-flat.svg`

**特点**:
- 大勾号设计，简洁直观
- 装饰节点代表 AI 智能
- 扁平化风格，时尚现代
- 小尺寸显示清晰

**使用场景**: 移动端应用、小尺寸展示、极简风格设计

---

## 🔧 实用变体

### 简化图标 (Simple Icon)

**文件**: `icon-simple.svg` (128x128)

**特点**:
- 极简设计，适合小尺寸
- 圆形底纹，适配各种背景
- 仅保留核心元素

**使用场景**:
- Favicon (网站图标)
- 通知图标
- Dock/任务栏图标
- 移动应用图标

---

### 横版横幅 (Horizontal Banner)

**文件**: `banner-horizontal.svg` (800x200)

**特点**:
- 左侧图标 + 右侧文字
- 包含副标题和功能标签
- 适合宽屏展示

**使用场景**:
- GitHub README 顶部横幅
- 文档页面 Header
- 邮件签名
- 社交媒体封面图

---

## 📊 使用建议

| 场景 | 推荐方案 | 尺寸建议 |
|------|---------|---------|
| Telegram Bot 头像 | Shield Bot | 512x512 |
| GitHub README | Horizontal Banner | 800x200 |
| 网站 Favicon | Simple Icon | 32x32, 64x64, 128x128 |
| 文档封面 | Modern Badge | 512x512 |
| 移动应用 | Minimal Flat | 1024x1024 |
| 社交媒体头像 | Shield Bot 或 Minimal Flat | 400x400 |

---

## 🎨 颜色规范

### 主色调
- **Telegram 蓝**: `#0088cc` - 品牌主色
- **成功绿**: `#00d4aa` - 验证通过、安全状态
- **深蓝**: `#005580` - 阴影和渐变

### 辅助色
- **白色**: `#ffffff` - 图标前景、文字
- **中性灰**: `#666666` - 副标题、说明文字
- **浅灰**: `#f5f5f5` - 背景装饰

---

## 📦 文件清单

### SVG 源文件（矢量格式）
```
assets/logo/
├── logo-shield-bot.svg        # 方案1: 盾牌机器人
├── logo-modern-badge.svg      # 方案2: 现代徽章
├── logo-minimal-flat.svg      # 方案3: 极简扁平
├── icon-simple.svg            # 简化图标
├── banner-horizontal.svg      # 横版横幅
└── README.md                  # 本文档
```

### PNG 导出文件（已生成）

**主 Logo (512×512)**
```
├── logo-shield-bot.png        # 盾牌机器人 (31KB)
├── logo-modern-badge.png      # 现代徽章 (66KB)
├── logo-minimal-flat.png      # 极简扁平 (32KB)
└── icon-simple.png            # 简化图标 (27KB)
```

**高清版本 (1024×1024)**
```
├── logo-shield-bot-1024.png       # 盾牌机器人高清版 (68KB)
├── logo-modern-badge-1024.png     # 现代徽章高清版 (159KB)
└── logo-minimal-flat-1024.png     # 极简扁平高清版 (78KB)
```

**Favicon 尺寸系列**
```
├── icon-16x16.png             # 643B
├── icon-32x32.png             # 1.2KB
├── icon-64x64.png             # 2.6KB
├── icon-128x128.png           # 5.2KB
└── icon-256x256.png           # 11KB
```

**横幅**
```
├── banner-horizontal.png      # 800×200 (28KB)
└── banner-horizontal-512.png  # 512×512 正方形版本 (37KB)
```

---

## 🚀 快速使用

### 1. 更新 GitHub README

在项目 `README.md` 顶部添加横幅：

```markdown
![Telegram Guard Bot](assets/logo/banner-horizontal.svg)
```

### 2. 设置 Bot 头像

直接使用已导出的 PNG 文件：

```bash
# 方式1: 在 Telegram 中手动上传
# 找到 BotFather -> /setuserpic -> 上传 logo-shield-bot.png

# 方式2: 使用 Telegram Bot API (需要 BOT_TOKEN)
curl -F "photo=@assets/logo/logo-shield-bot.png" \
  "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setUserProfilePhoto"
```

推荐使用 **logo-shield-bot.png** (512×512)

### 3. 使用 Favicon

已生成多种尺寸的 Favicon，可直接使用：

**方法1: 直接使用 PNG 文件**
```html
<link rel="icon" type="image/png" sizes="32x32" href="assets/logo/icon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="assets/logo/icon-16x16.png">
```

**方法2: 生成 ICO 文件（可选）**

使用在线工具将多尺寸 PNG 合并为 .ico：
- https://www.icoconverter.com/
- https://favicon.io/

上传以下文件：
- icon-16x16.png
- icon-32x32.png
- icon-64x64.png

---

## 📝 设计规范

### 最小尺寸限制
- Logo 最小显示尺寸: **48x48 px**
- 文字最小显示尺寸: **16pt**

### 安全区域
- Logo 周围保持至少 **20%** 的留白空间

### 格式导出建议
- **SVG**: 矢量格式，适合所有场景（优先推荐）
- **PNG**: 透明背景，网站/应用使用
  - 小图标: 128x128, 256x256
  - 标准: 512x512
  - 高清: 1024x1024, 2048x2048
- **ICO**: Windows Favicon，包含 16x16, 32x32, 64x64

---

## 🛠️ 修改建议

如果需要定制 Logo：

1. **编辑 SVG**: 使用 Figma / Inkscape / Adobe Illustrator
2. **调整颜色**: 修改 `<linearGradient>` 和 `fill` 属性
3. **导出多格式**: 使用 SVG → PNG 转换工具

---

## 📄 许可证

本 Logo 设计遵循项目开源协议，可自由使用和修改。

---

**设计时间**: 2026-01-11
**版本**: v1.0
**设计工具**: SVG (手写代码)
