# V2 数据库迁移与重构指南

## 一、变更概览

### 新增文件
| 文件 | 作用 |
|------|------|
| `src/aggregator/translator.py` | 泰中翻译服务（缓存优先 + LLM 兜底） |
| `src/aggregator/topic_cluster.py` | 话题聚类（共现矩阵 + 贪心分组） |
| `src/aggregator/dedup.py` | 消息去重（SHA-256 指纹）+ 质量评分 |
| `alembic/` | Alembic 迁移框架 |
| `alembic/versions/001_v2_schema_upgrade.py` | V2 schema 迁移脚本 |

### 修改文件
| 文件 | 变更内容 |
|------|----------|
| `src/storage/models.py` | 新增 3 个 model (KeywordTranslation, DailyDigest, AnalysisTask)；Message 增加 3 个字段 |
| `src/storage/db.py` | 新增 15+ CRUD 方法（翻译缓存、每日摘要、异步任务、趋势查询等） |
| `src/storage/__init__.py` | 导出新 model |
| `src/aggregator/scheduler.py` | 集成翻译、聚类、去重；新增每日摘要生成、异步任务执行 |
| `src/aggregator/llm_summary.py` | 新增 `_extract_json_array()` 方法 |
| `src/collector/client.py` | 集成去重器，采集时计算 content_hash 和 quality_score |
| `src/api/app.py` | 新增 7 个 V2 API 端点（异步分析 + 结构化 JSON） |
| `src/main.py` | 新增 `digest` 和 `migrate` 子命令 |
| `requirements.txt` | 新增 `alembic>=1.13.0` |

---

## 二、数据库 Schema 变更

### 新增表 1: `keyword_translations`（关键词翻译缓存）
```
keyword_thai  VARCHAR(255)  PK     -- 泰语原词
keyword_cn    VARCHAR(255)         -- 中文翻译
keyword_en    VARCHAR(255)  NULL   -- 英文翻译（备用）
category      VARCHAR(64)   NULL   -- 分类标签
created_at    TIMESTAMPTZ          -- 创建时间
```
**用途**：泰语关键词只翻译一次，后续直接查缓存，避免重复调 LLM。

### 新增表 2: `daily_digests`（每日中文摘要）
```
id              SERIAL      PK
digest_date     TIMESTAMPTZ UNIQUE -- 摘要日期
timezone        VARCHAR(64)        -- 时区
total_messages  INT                -- 当日总消息数
thai_messages   INT                -- 泰语消息数
active_users    INT                -- 活跃用户数
summary_cn      TEXT               -- 200字中文日报
top_topics      JSON               -- 话题聚类结果
demand_signals  JSON               -- 需求信号（中文）
keyword_cloud   JSON               -- 词云数据
hourly_volumes  JSON               -- 24小时消息量曲线
created_at      TIMESTAMPTZ
```
**用途**：回答"今天泰语区聊了什么"的一站式数据。

### 新增表 3: `analysis_tasks`（异步任务队列）
```
task_id     VARCHAR(36)  PK    -- UUID
status      VARCHAR(20)        -- pending/running/done/failed
mode        VARCHAR(20)        -- hourly/today/daily_digest
progress    INT                -- 0-100
result      JSON         NULL  -- 完成后的结果
error       TEXT         NULL  -- 失败原因
created_at  TIMESTAMPTZ
updated_at  TIMESTAMPTZ
```
**用途**：长耗时分析后台执行，前端轮询进度。

### 修改表: `messages`（新增 3 列）
```
content_hash   VARCHAR(64)  NULL  INDEX  -- 内容 SHA-256 指纹（前16位）
is_duplicate   BOOLEAN      DEFAULT false INDEX  -- 是否重复消息
quality_score  FLOAT        DEFAULT 1.0  -- 消息质量评分 (0-1)
```
**用途**：去重降低 LLM 成本，质量过滤提升分析精度。

---

## 三、数据库迁移操作指南

### 场景 A：全新部署（没有现有数据）

最简单的方式，直接用 `init-db` 创建所有表：

```bash
# 设置数据库连接
export DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/discord_thai"

# 创建所有表
python -m src.main init-db

# 标记 Alembic 为最新版本（这样后续迁移不会重复执行）
alembic stamp head
```

### 场景 B：生产环境迁移（已有数据，不能丢）

**这是你最关心的场景。** 步骤如下：

#### 步骤 1: 备份数据库

```bash
# 完整备份
pg_dump -h localhost -U postgres -d discord_thai -F custom -f backup_before_v2.dump

# 或者只备份 schema
pg_dump -h localhost -U postgres -d discord_thai --schema-only -f schema_backup.sql
```

#### 步骤 2: 配置 Alembic 连接

方法一：通过环境变量（推荐）
```bash
export DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/discord_thai"
```

方法二：编辑 `alembic.ini`
```ini
sqlalchemy.url = postgresql+psycopg2://user:pass@localhost:5432/discord_thai
```

#### 步骤 3: 标记当前数据库状态

因为现有表是用 `create_all()` 创建的，不是 Alembic 管理的，
需要先创建 `alembic_version` 表并标记为"迁移前"状态：

```bash
# 如果数据库里还没有 alembic_version 表，先用 SQL 创建：
psql -h localhost -U postgres -d discord_thai -c "
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
"

# 不要 stamp head！因为我们需要执行迁移脚本来添加新表和新列
```

#### 步骤 4: 执行迁移

```bash
# 先预览将要执行的 SQL（不实际执行）
alembic upgrade head --sql

# 确认无误后，执行迁移
alembic upgrade head
```

这个迁移会：
- 创建 `keyword_translations` 表
- 创建 `daily_digests` 表
- 创建 `analysis_tasks` 表
- 在 `messages` 表上添加 `content_hash`, `is_duplicate`, `quality_score` 三列
- 创建相关索引

**所有操作都是 ADD（新增），不会修改或删除任何现有数据。**

#### 步骤 5: 为已有消息回填新字段（可选）

迁移后，旧消息的 `content_hash` 为 NULL，`quality_score` 为 1.0。
如果你想回填这些值：

```sql
-- 回填 content_hash（简单版，用 MD5 代替 Python 的 SHA-256）
UPDATE messages
SET content_hash = LEFT(MD5(LOWER(COALESCE(cleaned_text, content))), 16)
WHERE content_hash IS NULL;

-- 标记重复消息
WITH dupes AS (
    SELECT content_hash, MIN(message_id) AS keep_id
    FROM messages
    WHERE content_hash IS NOT NULL
    GROUP BY content_hash
    HAVING COUNT(*) > 1
)
UPDATE messages m
SET is_duplicate = true
FROM dupes d
WHERE m.content_hash = d.content_hash
  AND m.message_id != d.keep_id;
```

#### 步骤 6: 验证

```bash
# 检查新表是否存在
psql -h localhost -U postgres -d discord_thai -c "\dt keyword_translations"
psql -h localhost -U postgres -d discord_thai -c "\dt daily_digests"
psql -h localhost -U postgres -d discord_thai -c "\dt analysis_tasks"

# 检查 messages 表新字段
psql -h localhost -U postgres -d discord_thai -c "\d messages"

# 检查 Alembic 版本
alembic current
```

### 场景 C：回滚（如果迁移出问题）

```bash
# 回滚到迁移前
alembic downgrade -1

# 或者从备份恢复
pg_restore -h localhost -U postgres -d discord_thai --clean backup_before_v2.dump
```

---

## 四、新增 API 端点

### V2 API（新增，所有 V1 API 保持不变）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v2/dashboard` | GET | 一次获取全部前端数据（中文） |
| `/api/v2/daily-digest?date=2026-03-23` | GET | 每日中文摘要 |
| `/api/v2/trends?days=7` | GET | 关键词和信号趋势 |
| `/api/v2/translations` | GET | 已缓存的翻译列表 |
| `/api/v2/analysis/run` | POST | 异步触发分析（返回 task_id） |
| `/api/v2/analysis/tasks/{id}` | GET | 查询异步任务进度 |

### V1 API（全部保留，不变）

所有现有 `/api/*` 端点保持不变，前端可以按需迁移到 V2。

---

## 五、新增 CLI 命令

```bash
# 生成每日摘要
python -m src.main digest --timezone Asia/Bangkok

# 运行数据库迁移
python -m src.main migrate
```

---

## 六、系统运转方式

### 数据流（V2 增强版）

```
Discord 消息 → Collector
   ↓
   计算 content_hash + quality_score（新增）
   ↓
   写入 messages 表
   ↓
每小时 → HourlyAggregator
   ↓
   过滤重复消息 + 低质量消息（新增）
   ↓
   TF-IDF 分析 → 关键词
   ↓
   需求信号提取（40+ 模式词，含子分类）（增强）
   ↓
   话题聚类（新增）
   ↓
   关键词翻译 → 中文（新增，缓存优先）
   ↓
   LLM 分层摘要（现有）
   ↓
   写入 hourly_keywords + analysis_runs
   ↓
每天 UTC 00:00 → DailyDigest
   ↓
   汇总当天所有小时数据
   ↓
   LLM 生成 200 字中文日报（新增）
   ↓
   写入 daily_digests 表
   ↓
前端 → /api/v2/dashboard
   ↓
   一次请求获取全部中文数据
```

### 进程模型（不变）

仍然是 4 个独立进程：
1. **Collector** (`python -m src.main collector`) - Discord 消息采集
2. **Scheduler** (`python -m src.main scheduler`) - 每小时聚合 + 每日摘要
3. **Web** (`python -m src.main web`) - FastAPI 服务
4. **一次性任务** (`python -m src.main aggregate/digest`) - 手动触发

---

## 七、后续迁移（未来新增字段时怎么做）

以后需要修改数据库 schema 时：

```bash
# 1. 修改 src/storage/models.py

# 2. 生成迁移脚本（自动对比 model 和数据库差异）
alembic revision --autogenerate -m "描述你的变更"

# 3. 检查生成的迁移文件
# 文件在 alembic/versions/ 目录下

# 4. 执行迁移
alembic upgrade head

# 5. 如果需要回滚
alembic downgrade -1
```

**Alembic 的好处**：
- 每次变更都有版本记录
- 支持回滚
- 可以先预览 SQL 再执行
- 多人协作时可以合并迁移
- 生产环境安全可控
