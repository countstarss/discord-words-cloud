from __future__ import annotations

import argparse
import os
from typing import Optional

from ..common import load_config
from ..storage import Database, init_db
from .client import DiscordCollector


# MARK: - Config
def _database_url_from_config(config: dict) -> Optional[str]:
    db_cfg = config.get("database", {})
    if db_cfg.get("url"):
        return db_cfg["url"]

    host = db_cfg.get("host")
    port = db_cfg.get("port")
    name = db_cfg.get("name")
    user = db_cfg.get("user")
    password = db_cfg.get("password")
    if not all([host, port, name, user]):
        return None
    password = password or ""
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


# MARK: - Runner
# collector 进程入口：读取配置 -> 初始化数据库 -> 启动 Discord 客户端。
def run_bot(config_path: Optional[str] = None) -> None:
    config = load_config(config_path)
    token = config.get("discord", {}).get("token") or os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Missing Discord token. Set discord.token or DISCORD_BOT_TOKEN")

    database_url = _database_url_from_config(config)
    db = init_db(database_url=database_url)

    client = DiscordCollector(config=config, db=db)
    client.run(token)


# MARK: - Main
def main() -> None:
    parser = argparse.ArgumentParser(description="Run Discord collector bot")
    parser.add_argument("--config", default=None, help="Config file path")
    args = parser.parse_args()
    run_bot(config_path=args.config)


if __name__ == "__main__":
    main()
