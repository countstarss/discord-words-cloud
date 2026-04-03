from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("dashboard", __name__)


@bp.get("/")
@bp.get("/dashboard")
def index() -> str:
    return render_template(
        "dashboard/index.html",
        active_nav="dashboard",
        page_kicker="Flask Workspace",
        page_title="Dashboard",
        page_description="",
        page_badges=[],
    )
