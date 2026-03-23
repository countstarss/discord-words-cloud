# Discord Thai Collector 部署方案（性价比优先）

## 1. 目标与约束

- 目标吞吐：约 `4 条消息/秒`（约 `34.5 万条/天`）
- 交付要求：长期在线 Web 状态页 + 定时分析 + 可维护成本低
- 优先级：`稳定 > 成本 > 复杂度`

---

## 2. 推荐方案（高性价比）

### 方案 A：单机一体化（推荐先上）

适合阶段：MVP 到小规模生产（你当前阶段）

- 1 台云主机（建议 `2 vCPU / 4 GB RAM / 80 GB SSD` 起）
- 1 个 `PostgreSQL`（同机 Docker 容器）
- 3 个应用进程（容器化）
  - `collector`
  - `scheduler`
  - `web`
- 1 个反向代理（`Caddy` 或 `Nginx`）负责 HTTPS

优势：
- 成本最低
- 架构简单，排障快
- 现在代码已经按三进程拆好，直接映射

风险：
- 单点故障
- 数据库与应用共享资源，峰值期抖动明显

### 方案 B：一体化应用 + 托管数据库（稳定版）

适合阶段：消息量进一步增长或老板要求更稳定

- 应用仍是 1 台 VM（collector/scheduler/web）
- PostgreSQL 改用托管（Neon / Supabase / 其他）

优势：
- 数据库运维压力显著下降（备份、恢复、监控）
- 应用主机可更激进压缩成本

风险：
- 月成本略高于方案 A
- 需要控制跨区网络延迟

---

## 3. 参考价格与预算区间（2026-03 可查公开页）

> 价格会变动，以下用于估算区间，不作为固定报价。

- DigitalOcean Droplet 起步价：`$4/月` 起，且按秒计费（有月封顶）  
  参考：<https://www.digitalocean.com/pricing/droplets>
- AWS Lightsail 示例：`$5/月`（1GB Linux bundle）  
  参考：<https://aws.amazon.com/lightsail/pricing/>
- Hetzner 价格调整文档（示例：CPX11 从 €4.49 到 €5.99，CPX22 从 €11.99 到 €15.99）  
  参考：<https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/>
- Railway Hobby：月订阅含 `$5 usage`，超出按差额计费  
  参考：<https://railway.com/pricing>
- Render Free 限制：空闲 15 分钟休眠、750 免费小时、Free Postgres 30 天到期（不建议生产）  
  参考：<https://render.com/docs/free>

### 预算建议

- 极致成本（可接受单点）：`$8 ~ $25/月`
  - 1 台 VM 跑全套 + 自托管 PG
- 稳定优先（性价比平衡）：`$30 ~ $80/月`
  - 1 台应用 VM + 托管 PG
- 可用性优先（建议后期）：`$100+/月`
  - 双机或多机 + 托管 PG + 备份/告警完善

---

## 4. 生产部署落地（建议你直接按这个执行）

### 4.1 容器与进程编排

建议使用 `docker compose` 部署 5 个服务：

- `collector`：`python -m src.main collector`
- `scheduler`：`python -m src.main scheduler`
- `web`：`python -m src.main web`
- `postgres`
- `caddy`（TLS + 反代）

### 4.2 配置与密钥

必须配置：

- `DISCORD_BOT_TOKEN`
- `DATABASE_URL`
- `CREDENTIALS_ENCRYPTION_KEY`

可选：

- `OPENAI_API_KEY`（若你不从 Web 配置供应商）

### 4.3 数据与备份

- Postgres 每日逻辑备份（`pg_dump`）
- 保留策略：
  - 原始消息 7~30 天
  - 聚合结果长期保留
- 每周做一次恢复演练（至少恢复到测试库）

### 4.4 监控与告警

最小监控：

- `/api/health`
- `/api/services` 三服务状态
- DB 可用性
- 磁盘使用率（>80% 告警）

---

## 5. 我建议你的执行顺序

1. 先上 `方案 A`，用最低成本跑真实流量 1~2 周
2. 观察三项指标：
   - 消息写入延迟
   - 每小时聚合耗时
   - 数据库 CPU/IO 峰值
3. 若任一指标持续超阈值，再切 `方案 B`（先迁库，后扩计算）

这条路径总体迁移成本最低，且与你当前代码结构完全对齐。
