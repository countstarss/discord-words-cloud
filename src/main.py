# Main entry point for Rubii Words Cloud
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import database_url_from_config, load_config


def run_db_migrations(config_path: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    config = load_config(config_path)
    database_url = database_url_from_config(config)
    alembic_ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"

    alembic_config = Config(str(alembic_ini_path))
    if database_url:
        alembic_config.set_main_option("sqlalchemy.url", database_url)

    print("Applying database migrations...")
    command.upgrade(alembic_config, "head")
    print("Database migrations applied.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rubii Words Cloud")
    parser.add_argument(
        "command",
        choices=[
            "bot",
            "api",
            "flask-web",
            "init-db",
            "migrate-db",
            "daily-report-worker",
            "daily-report-once",
            "daily-report-date",
            "channel-daily-report-worker",
        ],
        help="Command to run",
    )
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--now", action="store_true", help="Generate report for today up to current time")
    parser.add_argument("--date", default=None, help="Generate report for a specific report date (YYYY-MM-DD)")
    parser.add_argument("--channel-id", type=int, default=None, help="Target channel id")
    args = parser.parse_args()

    if args.command == "bot":
        from .collector.bot import run_bot
        run_bot(config_path=args.config)
    elif args.command == "api":
        from .api.app import run_web
        run_web(config_path=args.config)
    elif args.command == "flask-web":
        from .web.flask_app import run_web as run_flask_web

        run_flask_web(config_path=args.config)
    elif args.command == "init-db":
        from .storage import init_db

        config = load_config(args.config)
        db_cfg = config.get("database", {})
        database_url = database_url_from_config(config)
        name = db_cfg.get("name", "rubii_words")

        print(f"Initializing database: {name}")
        init_db(database_url=database_url)
        print("Database initialized successfully!")
    elif args.command == "migrate-db":
        run_db_migrations(config_path=args.config)
    elif args.command == "daily-report-worker":
        from .reports import run_daily_report_worker

        run_daily_report_worker(config_path=args.config)
    elif args.command == "daily-report-once":
        if not args.now:
            parser.error("daily-report-once requires --now")
        from .reports import run_daily_report_once_now

        run_daily_report_once_now(config_path=args.config)
    elif args.command == "daily-report-date":
        if not args.date:
            parser.error("daily-report-date requires --date YYYY-MM-DD")
        from .reports import run_daily_report_for_date

        run_daily_report_for_date(date_value=args.date, config_path=args.config)
    elif args.command == "channel-daily-report-worker":
        if not args.channel_id:
            parser.error("channel-daily-report-worker requires --channel-id")
        from .reports import run_channel_daily_report_worker

        run_channel_daily_report_worker(channel_id=args.channel_id, config_path=args.config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
