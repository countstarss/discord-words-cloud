# Rubii Words Cloud

A Discord message collector and API service that stores messages and exposes them via REST API.

## Features

- **Discord Collector**: Collects messages from Discord servers/channels in real-time
- **Database Storage**: Stores messages in PostgreSQL with deduplication and quality scoring
- **REST API**: Exposes message data via FastAPI

## Quick Start

### 1. Install dependencies

```bash
cd rubii-words-cloud
cp .env.example .env
# Edit .env with your configuration

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure

Edit `.env` with your settings:

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=rubii_words
DB_USER=postgres
DB_PASSWORD=your_password

DISCORD_BOT_TOKEN=your_discord_bot_token
```

For future multi-region, multi-channel collection, we recommend configuring named region groups directly in `.env`:

```env
DISCORD_REGION_CHANNELS=[
  {"key":"cn","name":"中国","guild_id":1283101973045841952,"channels":[
    {"id":1400146275512352799,"name":"频道标题1"},
    {"id":1400146275512352800,"name":"频道标题2"},
    {"id":1400146275512352801,"name":"频道标题3"}
  ]},
  {"key":"th","name":"泰国","guild_id":1483900000000000000,"channels":[
    {"id":1483900000000000001,"name":"频道标题1"},
    {"id":1483900000000000002,"name":"频道标题2"},
    {"id":1483900000000000003,"name":"频道标题3"}
  ]}
]
```

This will be normalized into a grouped target map for the collector and dashboard:

- 中国
  - 频道标题1
  - 频道标题2
  - 频道标题3
- 泰国
  - 频道标题1
  - 频道标题2
  - 频道标题3

The legacy flat configuration still works if you only want raw IDs, but it is now considered compatibility mode:

```yaml
targets:
  guild_ids: [123456789, ]
  channel_ids: [123456789, ]
```

### 3. Initialize Database

**Option A - Quick init (creates all tables):**
```bash
python3 -m src.main init-db
```

**Option B - Using Alembic (recommended for migrations):**
```bash
# Generate a migration
alembic revision --autogenerate -m "description"

# Run migrations
alembic upgrade head
```

### 4. Run

**Start API server:**
```bash
python3 -m src.main api
# or
python3 -m src.api.app
```

Open the dashboard at [http://127.0.0.1:8080/](http://127.0.0.1:8080/).

**Start Flask web server:**
```bash
python3 -m src.main flask-web
# or
python3 -m src.web.flask_app
```

The first Flask version serves the same dashboard UI and API payloads as the current FastAPI page, while giving us a separate web layer for future UI expansion.

**Start Discord bot:**
```bash
python3 -m src.main bot
# or
python3 -m src.collector.bot
```

**Start daily report worker:**
```bash
python3 -m src.main daily-report-worker
```
This worker now:
- builds one `hourly_reports` record every 2 hours at `HH:05` Asia/Shanghai
- merges the previous day's 12 interval reports into one daily report at `00:20` Asia/Shanghai
- uses `reporting.llm` config to control rolling 5-hour quota usage, shard sizing, and parallel shard requests

**Generate today's report up to now:**
```bash
python3 -m src.main daily-report-once --now
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/status` | Get statistics |
| `GET /api/messages` | List messages |
| `GET /api/messages/{id}` | Get single message |
| `GET /api/stats` | Get detailed stats |
| `GET /api/dashboard` | Dashboard summary |
| `GET /daily-report` | List all daily reports |
| `GET /` | Dashboard web UI |

## Configuration

See `config/config.yaml` for all configuration options.

Target configuration notes:

- `DISCORD_REGION_CHANNELS` is the recommended future-proof option when you need multiple regions and named channels.
- Each region supports `key`, `name`, `guild_id` or `guild_ids`, and a `channels` list.
- Each channel supports `id`, `name`, and optional `guild_id` or `guild_ids`.
- `TARGET_GUILD_IDS` and `TARGET_CHANNEL_IDS` still work for compatibility, but `DISCORD_REGION_CHANNELS` should be treated as the primary configuration entry going forward.
