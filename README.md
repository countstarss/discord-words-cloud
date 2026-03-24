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
python -m venv venv
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
python -m src.main init-db
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
python -m src.main api
# or
python -m src.api.app
```

**Start Discord bot:**
```bash
python -m src.main bot
# or
python -m src.collector.bot
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/status` | Get statistics |
| `GET /api/messages` | List messages |
| `GET /api/messages/{id}` | Get single message |
| `GET /api/stats` | Get detailed stats |

## Configuration

See `config/config.yaml` for all configuration options.
