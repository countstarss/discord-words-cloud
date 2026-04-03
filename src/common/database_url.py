from __future__ import annotations

import os
from typing import Optional


def normalize_database_url(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("postgresql+psycopg2://"):
        return raw
    if raw.startswith("postgresql://"):
        return f"postgresql+psycopg2://{raw[len('postgresql://'):]}"
    if raw.startswith("postgres://"):
        return f"postgresql+psycopg2://{raw[len('postgres://'):]}"
    return raw


def database_url_from_config(config: dict) -> Optional[str]:
    db_cfg = config.get("database", {})
    if db_cfg.get("url"):
        return normalize_database_url(str(db_cfg["url"]))

    env_database_url = normalize_database_url(os.getenv("DATABASE_URL"))
    if env_database_url:
        return env_database_url

    host = db_cfg.get("host")
    port = db_cfg.get("port")
    name = db_cfg.get("name")
    user = db_cfg.get("user")
    password = db_cfg.get("password")
    if not all([host, port, name, user]):
        return None
    password = password or ""
    return normalize_database_url(f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}")
