# Collector bot entry point
from __future__ import annotations

import argparse
import os
from typing import Optional

from ..common import database_url_from_config, load_config
from ..storage import init_db
from .client import DiscordCollector


# MARK: - Runner
def run_bot(config_path: Optional[str] = None) -> None:
    config = load_config(config_path)
    token = config.get("discord", {}).get("token") or os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Missing Discord token. Set discord.token or DISCORD_BOT_TOKEN")

    database_url = database_url_from_config(config)
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
