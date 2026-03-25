from __future__ import annotations

import argparse
from typing import Optional

from flask import Flask, Response, request

from ..api.app import (
    _dashboard_html,
    get_daily_reports as api_get_daily_reports,
    get_dashboard as api_get_dashboard,
    get_message as api_get_message,
    get_messages as api_get_messages,
    get_stats as api_get_stats,
    health as api_health,
    status as api_status,
)
from ..common import load_config
from ..storage import init_db


def _int_arg(name: str, default: int) -> int:
    value = request.args.get(name, type=int)
    return default if value is None else value


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def dashboard() -> Response:
        return Response(_dashboard_html(), mimetype="text/html")

    @app.get("/api/health")
    def health() -> dict:
        return api_health()

    @app.get("/api/status")
    def status() -> dict:
        hours = _int_arg("hours", 24)
        return api_status(hours=hours)

    @app.get("/api/messages")
    def messages() -> dict:
        limit = _int_arg("limit", 100)
        offset = _int_arg("offset", 0)
        scope_key = request.args.get("scope_key")
        return api_get_messages(limit=limit, offset=offset, scope_key=scope_key)

    @app.get("/api/messages/<int:message_id>")
    def message(message_id: int) -> dict:
        return api_get_message(message_id)

    @app.get("/api/stats")
    def stats() -> dict:
        hours = _int_arg("hours", 24)
        scope_key = request.args.get("scope_key")
        return api_get_stats(hours=hours, scope_key=scope_key)

    @app.get("/api/dashboard")
    def dashboard_payload() -> dict:
        return api_get_dashboard()

    @app.get("/daily-report")
    def daily_report() -> dict:
        scope_key = request.args.get("scope_key", "global")
        return api_get_daily_reports(scope_key=scope_key)

    return app


app = create_app()


def run_web(config_path: Optional[str] = None) -> None:
    config = load_config(config_path)

    db_cfg = config.get("database", {})
    database_url = None
    if db_cfg.get("url"):
        database_url = db_cfg["url"]
    else:
        host = db_cfg.get("host", "localhost")
        port = db_cfg.get("port", "5432")
        name = db_cfg.get("name", "rubii_words_cloud")
        user = db_cfg.get("user", "postgres")
        password = db_cfg.get("password", "")
        database_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"

    init_db(database_url=database_url)

    web_cfg = config.get("web", {})
    host_addr = web_cfg.get("host", "0.0.0.0")
    port_num = int(web_cfg.get("port", 8080))

    app.run(host=host_addr, port=port_num, debug=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Rubii Words Cloud Flask web server")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    run_web(config_path=args.config)


if __name__ == "__main__":
    main()
