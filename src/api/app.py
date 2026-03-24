# API application
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query
from pydantic import BaseModel

from ..common import load_config
from ..storage import get_db, init_db

app = FastAPI(title="Rubii Words Cloud", version="0.1.0")


# MARK: - Health & Status APIs
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def status(hours: int = Query(default=24, ge=1, le=168)) -> dict:
    db = get_db()
    return db.get_stats(hours=hours)


# MARK: - Message APIs
@app.get("/api/messages")
def get_messages(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    target_language_only: bool = Query(default=False),
) -> dict:
    db = get_db()
    messages = db.get_all_messages(
        limit=limit,
        offset=offset,
        target_language_only=target_language_only,
    )
    return {
        "messages": [
            {
                "message_id": m.message_id,
                "guild_id": m.guild_id,
                "channel_id": m.channel_id,
                "author_id": m.author_id,
                "content": m.content,
                "is_target_language": m.is_target_language,
                "language": m.language,
                "lang_confidence": m.lang_confidence,
                "cleaned_text": m.cleaned_text,
                "quality_score": m.quality_score,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/messages/{message_id}")
def get_message(message_id: int) -> dict:
    db = get_db()
    message = db.get_message_by_id(message_id)
    if message is None:
        return {"error": "Message not found"}
    return {
        "message_id": message.message_id,
        "guild_id": message.guild_id,
        "channel_id": message.channel_id,
        "author_id": message.author_id,
        "content": message.content,
        "is_target_language": message.is_target_language,
        "language": message.language,
        "lang_confidence": message.lang_confidence,
        "cleaned_text": message.cleaned_text,
        "tokens": message.tokens,
        "quality_score": message.quality_score,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "updated_at": message.updated_at.isoformat() if message.updated_at else None,
    }


# MARK: - Stats APIs
@app.get("/api/stats")
def get_stats(
    hours: int = Query(default=24, ge=1, le=168),
    target_language_only: bool = Query(default=False),
) -> dict:
    db = get_db()
    stats = db.get_stats(hours=hours)
    
    if target_language_only:
        stats["total_messages"] = db.count_messages(target_language_only=True)
        stats["target_language_only"] = True
    
    return stats


# MARK: - Web Runtime
def run_web(config_path: Optional[str] = None) -> None:
    config = load_config(config_path)
    
    # Initialize database
    db_cfg = config.get("database", {})
    database_url = None
    if db_cfg.get("url"):
        database_url = db_cfg["url"]
    else:
        host = db_cfg.get("host", "localhost")
        port = db_cfg.get("port", "5432")
        name = db_cfg.get("name", "rubii_words")
        user = db_cfg.get("user", "postgres")
        password = db_cfg.get("password", "")
        database_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
    
    init_db(database_url=database_url)

    web_cfg = config.get("web", {})
    host_addr = web_cfg.get("host", "0.0.0.0")
    port_num = int(web_cfg.get("port", 8080))

    uvicorn.run("src.api.app:app", host=host_addr, port=port_num, reload=False)


# MARK: - Main
def main() -> None:
    parser = argparse.ArgumentParser(description="Run Rubii Words Cloud API server")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    run_web(config_path=args.config)


if __name__ == "__main__":
    main()
