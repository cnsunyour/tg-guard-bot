# 生产部署安全检查清单

> **使用说明**：部署到生产环境前，请逐项检查并确认所有项目。

---

## ✅ 第一阶段：环境准备

### 1.1 服务器基础安全

- [ ] **操作系统**
  - [ ] 使用最新的 LTS 版本（Ubuntu 22.04 / Debian 12）
  - [ ] 已应用所有安全更新：`sudo apt update && sudo apt upgrade`
  - [ ] 禁用不必要的服务
  - [ ] 配置自动安全更新

- [ ] **防火墙配置**
  - [ ] 启用 UFW/iptables
  - [ ] 只开放必要端口（SSH: 22, HTTPS: 443）
  - [ ] 数据库端口（5432, 6379）**不对外开放**
  - [ ] 限制 SSH 访问源 IP（如可能）

- [ ] **SSH 安全**
  - [ ] 禁用 root 登录
  - [ ] 禁用密码登录，仅使用密钥
  - [ ] 修改默认端口（可选）
  - [ ] 配置 fail2ban

- [ ] **用户权限**
  - [ ] 创建专用非 root 用户运行应用
  - [ ] 配置 sudo 权限（最小权限原则）
  - [ ] 禁用不必要的用户账号

### 1.2 Docker 环境

- [ ] **Docker 安全**
  - [ ] 安装最新稳定版 Docker
  - [ ] Docker daemon 配置安全选项
  - [ ] 启用 Docker Content Trust（可选）
  - [ ] 定期清理未使用的镜像和容器

- [ ] **Docker Compose**
  - [ ] 安装最新版本
  - [ ] 配置日志驱动和轮转
  - [ ] 设置资源限制（CPU、内存）

---

## ✅ 第二阶段：应用配置

### 2.1 环境变量配置

- [ ] **创建 .env 文件**
  ```bash
  cp .env.example .env
  chmod 600 .env  # 限制文件权限
  ```

- [ ] **BOT_TOKEN**
  - [ ] 从 @BotFather 获取新的 token
  - [ ] **不使用**测试环境的 token
  - [ ] 格式验证：`数字:字母数字混合`

- [ ] **ADMIN_IDS**
  - [ ] 设置正确的管理员 Telegram ID
  - [ ] 多个 ID 用逗号分隔
  - [ ] 移除所有测试账号 ID

- [ ] **数据库密码（DB_PASSWORD）**
  - [ ] 长度 ≥ 16 字符
  - [ ] 包含大写、小写、数字、特殊字符
  - [ ] 使用随机生成：`openssl rand -base64 32`
  - [ ] **不使用**示例密码

- [ ] **Redis 密码（REDIS_PASSWORD）**
  - [ ] 长度 ≥ 16 字符
  - [ ] 包含大写、小写、数字、特殊字符
  - [ ] 使用随机生成：`openssl rand -base64 32`
  - [ ] **不使用**示例密码

- [ ] **模型签名密钥（MODEL_SIGNATURE_KEY）**
  - [ ] 使用随机生成：`openssl rand -hex 32`
  - [ ] 64 字符十六进制字符串
  - [ ] 生成后妥善保管，丢失将无法加载已训练模型

- [ ] **日志级别**
  - [ ] 生产环境设置为 `INFO`
  - [ ] **不使用** `DEBUG`（会记录敏感信息）

- [ ] **调试模式**
  - [ ] 设置 `DEBUG=false`

### 2.2 docker-compose.yml 检查

- [ ] **端口映射**
  - [ ] PostgreSQL 端口**未映射**到主机
  - [ ] Redis 端口**未映射**到主机
  - [ ] 确认注释掉了 ports 配置

- [ ] **用户权限**
  - [ ] bot 服务设置 `user: "1000:1000"`
  - [ ] 确认容器不以 root 运行

- [ ] **资源限制**（推荐）
  ```yaml
  deploy:
    resources:
      limits:
        cpus: '1.0'
        memory: 1G
  ```

- [ ] **重启策略**
  - [ ] 设置 `restart: unless-stopped`

- [ ] **Volume 权限**
  ```bash
  sudo chown -R 1000:1000 ./data/models
  sudo chown -R 1000:1000 ./logs
  chmod 755 ./data/models
  chmod 755 ./logs
  ```

---

## ✅ 第三阶段：安全加固

### 3.1 网络安全

- [ ] **反向代理（可选但推荐）**
  - [ ] 配置 Nginx 作为反向代理
  - [ ] 启用 HTTPS（Let's Encrypt）
  - [ ] 配置 rate limiting
  - [ ] 添加安全 headers

- [ ] **数据库访问**
  - [ ] 仅允许 Docker 网络内访问
  - [ ] 使用防火墙规则限制
  - [ ] 考虑使用 Unix socket（高级）

- [ ] **监控和告警**
  - [ ] 配置日志监控（ELK/Grafana Loki）
  - [ ] 设置异常告警
  - [ ] 监控资源使用率

### 3.2 应用安全

- [ ] **速率限制**
  - [ ] 确认中间件已启用
  - [ ] 根据实际负载调整限制值
  - [ ] 监控速率限制触发日志

- [ ] **日志安全**
  - [ ] 确认敏感信息已脱敏
  - [ ] 配置日志轮转（7天保留）
  - [ ] 错误日志单独存储（30天保留）
  - [ ] 定期审查日志内容

- [ ] **模型安全**
  - [ ] 首次训练后验证签名功能
  - [ ] 备份训练好的模型文件
  - [ ] 限制模型文件访问权限

### 3.3 备份策略

- [ ] **数据库备份**
  ```bash
  # 配置自动备份脚本
  0 2 * * * /path/to/backup.sh
  ```
  - [ ] 每日自动备份
  - [ ] 保留 7 天备份
  - [ ] 测试恢复流程

- [ ] **配置备份**
  - [ ] .env 文件加密备份
  - [ ] docker-compose.yml 备份
  - [ ] 训练模型备份

- [ ] **异地备份**
  - [ ] 配置远程备份（S3/其他云存储）
  - [ ] 加密传输
  - [ ] 定期验证备份完整性

---

## ✅ 第四阶段：部署验证

### 4.1 部署前测试

- [ ] **安全扫描**
  ```bash
  # 代码安全扫描
  bandit -r src/

  # 依赖漏洞检查
  safety check

  # 容器扫描
  trivy image tg-guard-bot:latest
  ```

- [ ] **配置验证**
  ```bash
  # 检查配置语法
  docker-compose config

  # 验证环境变量
  docker-compose run --rm bot python -c "from src.core.config import settings; print(settings)"
  ```

- [ ] **数据库连接测试**
  ```bash
  docker-compose up -d postgres redis
  docker-compose run --rm bot python scripts/migrate.py check
  ```

### 4.2 部署执行

- [ ] **构建镜像**
  ```bash
  docker-compose build --no-cache
  ```

- [ ] **数据库初始化**
  ```bash
  docker-compose up -d postgres redis
  sleep 10  # 等待数据库启动
  docker-compose run --rm bot python scripts/migrate.py init
  ```

- [ ] **启动服务**
  ```bash
  docker-compose up -d
  ```

- [ ] **查看日志**
  ```bash
  docker-compose logs -f bot
  ```

### 4.3 部署后验证

- [ ] **健康检查**
  - [ ] Bot 成功连接 Telegram
  - [ ] 数据库连接正常
  - [ ] Redis 连接正常
  - [ ] 无错误日志

- [ ] **功能测试**
  - [ ] 发送 `/start` 命令响应正常
  - [ ] 发送 `/health` 查看健康状态
  - [ ] 发送 `/stats` 查看统计信息
  - [ ] 测试入群验证流程
  - [ ] 测试反垃圾检测
  - [ ] 测试管理员命令权限

- [ ] **安全验证**
  - [ ] 非管理员无法执行管理命令
  - [ ] 速率限制正常触发
  - [ ] 数据库端口外部无法访问
  - [ ] Redis 端口外部无法访问

---

## ✅ 第五阶段：运维监控

### 5.1 日常监控

- [ ] **资源监控**
  - [ ] CPU 使用率 < 80%
  - [ ] 内存使用率 < 80%
  - [ ] 磁盘使用率 < 80%
  - [ ] 数据库连接数正常

- [ ] **日志监控**
  - [ ] 每日检查错误日志
  - [ ] 关注速率限制触发
  - [ ] 追踪异常行为模式
  - [ ] 审计管理员操作

- [ ] **性能监控**
  - [ ] 消息处理延迟
  - [ ] 垃圾检测准确率
  - [ ] 数据库查询性能
  - [ ] Redis 命中率

### 5.2 定期维护

- [ ] **每周**
  - [ ] 检查系统安全更新
  - [ ] 审查日志异常
  - [ ] 清理过期数据
  - [ ] 验证备份完整性

- [ ] **每月**
  - [ ] 运行安全扫描
    ```bash
    safety check
    bandit -r src/
    ```
  - [ ] 更新依赖包（测试后）
  - [ ] 审查权限配置
  - [ ] 检查证书有效期

- [ ] **每季度**
  - [ ] 全面安全审计
  - [ ] 性能优化评估
  - [ ] 备份恢复演练
  - [ ] 更新文档

- [ ] **每年**
  - [ ] 专业安全评估
  - [ ] 架构审查
  - [ ] 灾难恢复演练

---

## 📋 快速检查命令

```bash
# 1. 检查环境变量
grep -E "BOT_TOKEN|ADMIN_IDS|PASSWORD|KEY" .env

# 2. 检查容器状态
docker-compose ps

# 3. 检查日志错误
docker-compose logs bot | grep -i error | tail -20

# 4. 检查端口暴露（应该只有必要端口）
sudo netstat -tlnp | grep -E "(5432|6379)"

# 5. 检查文件权限
ls -la .env data/ logs/

# 6. 检查容器用户
docker-compose exec bot whoami  # 应该是 appuser

# 7. 健康检查
docker-compose exec bot python -c "from src.core.database import check_database; import asyncio; asyncio.run(check_database())"

# 8. 资源使用
docker stats --no-stream
```

---

## 🚨 紧急响应流程

### 安全事件响应

1. **发现安全问题**
   - [ ] 立即停止受影响的服务
   - [ ] 保存日志和证据
   - [ ] 隔离受影响系统

2. **评估影响**
   - [ ] 确定漏洞类型和范围
   - [ ] 识别受影响的数据
   - [ ] 评估风险级别

3. **修复措施**
   - [ ] 应用安全补丁
   - [ ] 重置受影响的凭据
   - [ ] 更新防护规则

4. **恢复服务**
   - [ ] 验证修复有效性
   - [ ] 逐步恢复服务
   - [ ] 加强监控

5. **事后分析**
   - [ ] 编写事件报告
   - [ ] 改进安全措施
   - [ ] 更新响应流程

---

## 📞 联系人

- **运维负责人**：[姓名] - [联系方式]
- **安全负责人**：[姓名] - [联系方式]
- **紧急联系**：[联系方式]

---

**检查人**：________________
**日期**：________________
**签名**：________________

---

**文档版本**：1.0
**最后更新**：2025-01-03
**下次审查**：2025-04-03
