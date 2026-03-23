# RUN（从 0 到可采集指定频道全量消息）

> 目标：让 Bot 采集某个服务器中“指定频道”的历史 + 实时消息，用于词云与需求分析。

## 1. 准备环境

```bash
cd /Users/luke/discord-thai-collector
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. PostgreSQL 准备

```sql
CREATE DATABASE discord_thai;
```

## 3. Discord 端必须配置（关键）

### 3.1 在 Developer Portal 配置 Bot

1. 创建 App 和 Bot（你已完成）
2. `Bot` 页面启用：`MESSAGE CONTENT INTENT`（必须）

如果没启用 Message Content Intent，Bot 拿不到 `message.content`，后续词云/需求挖掘会失效。

### 3.2 安装 Bot 到目标服务器

在 `OAuth2` 生成安装链接并安装，权限至少包含：

- `View Channel`
- `Read Message History`

如果是私密频道，还要在该频道权限覆盖里给 Bot 角色同样权限。

## 4. 获取 target（服务器ID / 频道ID）

1. Discord 客户端开启 Developer Mode：
   - `User Settings -> Advanced -> Developer Mode`
2. 右键服务器：`Copy Server ID`
3. 右键频道：`Copy Channel ID`

## 5. 配置 `.env`

复制 `.env.example` 为 `.env`，至少填：

说明：[`config/config.yaml`](/Users/luke/discord-thai-collector/config/config.yaml) 默认通过 `${ENV:default}` 从 `.env` / 当前 shell 环境取值，不再需要把敏感信息写在 YAML 明文里。

```bash
DISCORD_BOT_TOKEN=你的bot_token

DB_HOST=localhost
DB_PORT=5432
DB_NAME=discord_thai
DB_USER=postgres
DB_PASSWORD=你的密码

# 目标服务器/频道（逗号分隔）
TARGET_GUILD_IDS=123456789012345678
TARGET_CHANNEL_IDS=234567890123456789,345678901234567890

# 回填配置
BACKFILL_ENABLED=true
BACKFILL_LIMIT_PER_CHANNEL=100
BACKFILL_OLDEST_FIRST=true
```

说明：
- `TARGET_CHANNEL_IDS` 不为空时，只采集这些频道（最精准，推荐）
- `BACKFILL_LIMIT_PER_CHANNEL=100` 表示每个目标频道仅回填最近 100 条（推荐先这样）
- 需要全量历史时再改为 `0`
- 如需开启 LLM 摘要，可额外设置 `LLM_ENABLED=true`

## 6. 初始化数据库

```bash
python -m src.main init-db
```

## 7. 启动采集（会先历史回填，再实时监听）

```bash
python -m src.main collector
```

启动后会看到：
- `Target guild IDs: ...`
- `Target channel IDs: ...`
- `[backfill] completed channels=... messages=...`

## 8. 验证采集是否命中目标频道

### 8.1 看数据库是否有目标频道数据

```sql
SELECT channel_id, COUNT(*)
FROM messages
GROUP BY channel_id
ORDER BY COUNT(*) DESC;
```

### 8.2 看采集状态 API

启动 Web 后（第10步）访问：

- `GET /api/status`
- `GET /api/services`

`collector` 的状态里会有：
- `backfill_channels`
- `backfill_messages`
- `backfill_done`

## 9. 执行小时分析

先手动跑一次（默认按“当天”汇总）：

```bash
python -m src.main aggregate
```

如果要按旧逻辑仅分析“上一小时窗口”：

```bash
python -m src.main aggregate --mode hourly
```

指定时区做“当天”汇总（推荐与你本地一致）：

```bash
python -m src.main aggregate --mode today --timezone Asia/Shanghai
```

也可以在 Web 页面 `Hourly Insights` 区域点击：

- `Run Today`
- `Run Hourly`

前端按钮会直接调用 `POST /api/analysis/run`，并立即刷新结果。

此外在看板里 `中文洞察摘要` 模块支持：

- `Refresh`：规则版中文摘要（不耗 LLM）
- `Summarize With LLM`：使用你已保存且启用的 `llm_provider_credentials` 做中文洞察总结

持续自动分析：

```bash
python -m src.main scheduler
```

## 10. 启动 Web 看板

```bash
python -m src.main web
```

打开：`http://localhost:8080`

---

## 关键 target 代码说明（你要求重点解释）

### A) target 是在哪里确定的

文件：`src/collector/client.py`

- 初始化时读取：`config.targets.guild_ids` / `config.targets.channel_ids`
- 支持两种格式：
  - YAML 数组
  - `.env` 逗号分隔字符串

相关方法：
- `_parse_id_set(...)`：把配置转成 `Set[int]`
- `_should_process(...)`：每条消息都用 target 规则过滤

### B) 历史“全量回填”是在哪里做的

文件：`src/collector/client.py`

- `on_ready(...)` 中触发 `_run_backfill()`
- `_run_backfill()` 会对目标频道执行 `channel.history(...)`
- 回填出的消息也走同一套 `_build_payload(...)` + `db.upsert_message(...)`

### C) 如果不指定频道会怎样

- `TARGET_CHANNEL_IDS` 为空时：
  - 若指定 `TARGET_GUILD_IDS`，会采这些服务器下所有文本频道
  - 若 guild 也为空，会采 Bot 可见的所有服务器文本频道（不推荐生产）

---

## 常见问题（按你这个场景）

### 1) 看到运行了但没有消息

优先检查：
- Bot 是否真的在目标服务器里
- 目标频道权限是否给了 `View Channel` + `Read Message History`
- `TARGET_CHANNEL_IDS` 是否填错

### 2) 只有实时没有历史

检查：
- `BACKFILL_ENABLED=true`
- `BACKFILL_LIMIT_PER_CHANNEL` 是否 > 0（如 100）或设为 0 全量回填

### 3) 有消息但词云很少

这是正常的：聚合只对 `is_thai=true` 生效。先看 `messages` 表里 `is_thai` 占比是否低。
