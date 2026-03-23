# Discord Thai Collector

Discord 泰语区聊天高吞吐采集与小时级分析 MVP。

## 目标

- 实时采集 Discord 消息（create / update / delete）
- 识别泰语并做清洗、分词
- 每小时自动生成关键词和“需求信号”摘要
- 支持可选 LLM 分层摘要（分块 -> 汇总），避免全文直接喂模型
- Web 页面内置 LLM Key 管理（多供应商：OpenAI/OpenRouter/DeepSeek/xAI/Anthropic/Gemini）
- 交付可访问 Web 状态页（API + Dashboard）

## 当前架构

```text
Discord Gateway Bot
  -> PostgreSQL (messages)
  -> Hourly Aggregator (TF-IDF + demand signals)
  -> PostgreSQL (hourly_keywords + analysis_runs)
  -> FastAPI Dashboard (/ + /api/*)
```

## 技术选型

- 采集: `discord.py`
- 存储: `PostgreSQL + SQLAlchemy`
- 文本处理: `PyThaiNLP`
- 关键词分析: `scikit-learn (TF-IDF)`
- Web 交付: `FastAPI + Uvicorn`

## 目录结构

```text
src/
  collector/
    bot.py
    client.py
  processor/
    detector.py
    cleaner.py
    tokenizer.py
  aggregator/
    tfidf.py
    scheduler.py
  storage/
    db.py
    models.py
  api/
    app.py
  common/
    config.py
  main.py
```

## 快速启动

1. 安装依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. 配置环境变量

`config/config.yaml` 默认会从项目根目录的 `.env` 和当前 shell 环境读取配置，不需要再把 token、数据库连接和目标频道写进 YAML 明文。

```bash
export DISCORD_BOT_TOKEN="..."
export DB_HOST="localhost"
export DB_PORT="5432"
export DB_NAME="discord_thai"
export DB_USER="postgres"
export DB_PASSWORD="..."
export TARGET_GUILD_IDS="123456789012345678"
export TARGET_CHANNEL_IDS="234567890123456789,345678901234567890"
export BACKFILL_ENABLED="true"
export BACKFILL_LIMIT_PER_CHANNEL="100"
```

3. 初始化数据库

```bash
python -m src.main init-db
```

4. 启动采集

```bash
python -m src.main collector
```

5. 执行一次小时分析（可由 crontab/systemd 定时）

```bash
python -m src.main aggregate
```

> `aggregate` 默认是按当天（`analysis_timezone`）汇总；如果要仅看上一小时，使用 `--mode hourly`。

6. 启动 Web

```bash
python -m src.main web
```

访问 `http://localhost:8080`

详细的“指定服务器 + 指定频道 + 历史全量回填”操作手册见 [RUN.md](/Users/luke/discord-thai-collector/RUN.md)。

## 主要 API

- `GET /api/health`
- `GET /api/status?hours=24`
- `GET /api/keywords?hours=24&limit=200`
- `GET /api/runs?limit=24`
- `GET /api/services` 查看 collector/scheduler/web 心跳状态
- `POST /api/analysis/run` 在 Web/接口中立即触发一次分析（支持 today/hourly）
- `GET /api/insights/explain` 基于关键词/需求信号生成中文可读洞察（可选 LLM）
- `GET /api/llm/catalog` 供应商目录
- `GET /api/llm/providers` 已配置供应商（key 脱敏）
- `POST /api/llm/providers` 新增/更新供应商 key
- `POST /api/llm/providers/{provider}/enable` 启用/禁用供应商
- `DELETE /api/llm/providers/{provider}` 删除供应商
- `POST /api/compliance/delete` 按 `message_ids/author_ids` 删除或标记删除，支持自动重算受影响小时窗口
- `POST /api/compliance/purge` 按保留天数清理历史原始消息

## 合规提醒（必须）

- 仅使用 Bot 账号，禁止 self-bot
- 需要启用并获批 Message Content Intent（规模上来后）
- 按政策实现删除请求与数据最小化留存（已提供 compliance API）

## 可选 LLM 摘要

默认关闭。开启方式：

1. 设置环境变量 `OPENAI_API_KEY`
2. 设置环境变量 `LLM_ENABLED=true`

也可以直接在 Web 页面配置供应商 key（推荐）。建议设置 `CREDENTIALS_ENCRYPTION_KEY`，用于对数据库中的 key 做可逆加密存储。
