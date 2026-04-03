from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("messages", __name__)


@bp.get("/messages")
def index() -> str:
    return render_template(
        "messages/index.html",
        active_nav="messages",
        page_title="Messages",
    )
