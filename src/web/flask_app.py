from __future__ import annotations

import argparse
from typing import Optional

from flask import Flask

from ..common import load_config
from ..storage import init_db
from .blueprints import api_bp, dashboard_bp, reports_bp
from .navigation import APP_NAME, PRIMARY_NAV_ITEMS


def create_app() -> Flask:
    app = Flask(__name__)

    app.register_blueprint(api_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(reports_bp)

    @app.context_processor
    def inject_shell_context() -> dict:
        return {
            "app_name": APP_NAME,
            "primary_nav_items": PRIMARY_NAV_ITEMS,
        }

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
