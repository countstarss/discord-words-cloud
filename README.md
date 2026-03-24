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

Edit `config/config.yaml` to configure targets:

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
