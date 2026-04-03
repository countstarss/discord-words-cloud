from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("reports", __name__)


@bp.get("/reports")
def index() -> str:
    return render_template(
        "reports/index.html",
        active_nav="reports",
        page_kicker="Flask Workspace",
        page_title="Reports",
        page_description="Browse generated daily reports by scope, switch dates quickly, and inspect markdown output in a dedicated reading surface.",
        page_badges=["Daily report browser", "Markdown preview"],
    )

