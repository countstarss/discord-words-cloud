#!/usr/bin/env python3
from __future__ import annotations

import argparse
from typing import Optional

from src.aggregator.scheduler import HourlyAggregator
from src.api.app import run_web
from src.collector.bot import run_bot
from src.common import load_config
from src.storage import init_db


# MARK: - Config
# 从统一配置中拼接数据库连接串；若 config 已给 url，则优先使用。
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


# MARK: - Main
# 主入口负责三件事：
# 1) 解析子命令
# 2) 初始化数据库
# 3) 分发到 collector / aggregate / scheduler / web
def main() -> None:
    parser = argparse.ArgumentParser(description="Discord Thai Collector")
    parser.add_argument("--config", default=None, help="Path to config yaml")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("collector")
    aggregate_parser = sub.add_parser("aggregate")
    aggregate_parser.add_argument("--mode", choices=["today", "hourly"], default="today")
    aggregate_parser.add_argument("--timezone", default=None, help="IANA timezone, e.g. Asia/Shanghai")
    sub.add_parser("scheduler")
    sub.add_parser("web")

    args = parser.parse_args()
    config = load_config(args.config)
    db = init_db(database_url=_database_url_from_config(config))

    if args.command == "init-db":
        print("Database initialized")
        return

    if args.command == "collector":
        run_bot(config_path=args.config)
        return

    if args.command == "aggregate":
        aggregator = HourlyAggregator(db=db, config=config.get("aggregator", {}))
        timezone_name = args.timezone or config.get("aggregator", {}).get("analysis_timezone", "Asia/Shanghai")
        if args.mode == "today":
            result = aggregator.run_today(timezone_name=timezone_name)
            print(
                f"Aggregated {result.message_count} messages for TODAY({timezone_name}) "
                f"in UTC window {result.window_start.isoformat()} -> {result.window_end.isoformat()}"
            )
            return

        result = aggregator.run_once()
        print(
            f"Aggregated {result.message_count} messages in window "
            f"{result.window_start.isoformat()} -> {result.window_end.isoformat()}"
        )
        return

    if args.command == "scheduler":
        HourlyAggregator(db=db, config=config.get("aggregator", {})).run_forever()
        return

    if args.command == "web":
        run_web(config_path=args.config)
        return


if __name__ == "__main__":
    main()
