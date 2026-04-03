from __future__ import annotations

from ..common import database_url_from_config, load_config
from ..storage import init_db
from .flask_app import create_app


def create_wsgi_app():
    config = load_config()
    init_db(database_url=database_url_from_config(config))
    return create_app()


app = create_wsgi_app()
