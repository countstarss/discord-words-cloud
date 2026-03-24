# Main entry point for Rubii Words Cloud
from __future__ import annotations

import argparse
import sys
from typing import Optional


def main() -> None:
    parser = argparse.ArgumentParser(description="Rubii Words Cloud")
    parser.add_argument(
        "command",
        choices=["bot", "api", "init-db", "daily-report-worker", "daily-report-once"],
        help="Command to run",
    )
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--now", action="store_true", help="Generate report for today up to current time")
    args = parser.parse_args()

    if args.command == "bot":
        from .collector.bot import run_bot
        run_bot(config_path=args.config)
    elif args.command == "api":
        from .api.app import run_web
        run_web(config_path=args.config)
    elif args.command == "init-db":
        from .common.config import load_config
        from .storage import init_db
        
        config = load_config(args.config)
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
        
        print(f"Initializing database: {name}")
        db = init_db(database_url=database_url)
        print("Database initialized successfully!")
    elif args.command == "daily-report-worker":
        from .reports import run_daily_report_worker

        run_daily_report_worker(config_path=args.config)
    elif args.command == "daily-report-once":
        if not args.now:
            parser.error("daily-report-once requires --now")
        from .reports import run_daily_report_once_now

        run_daily_report_once_now(config_path=args.config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
