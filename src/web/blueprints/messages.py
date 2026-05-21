from __future__ import annotations

import csv
import re
from datetime import date
from io import StringIO
from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request

from ...storage import get_db
from ..api_payloads import _local_date_window, _message_browser_scopes

bp = Blueprint("messages", __name__)

MESSAGE_EXPORT_COLUMNS = [
    "created_at",
    "region_name",
    "channel_name",
    "message_id",
    "author_id",
    "detected_language",
    "content",
]


def _slugify(value: str, fallback: str = "messages") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or fallback


def _message_export_filename(scope: dict[str, Any], date_label: str, scope_key: str) -> str:
    country = str(scope.get("region_key") or scope.get("region_name") or scope_key)
    channel = str(scope.get("channel_name") or scope.get("label") or scope_key)
    return f"messages_{_slugify(country, 'country')}_{_slugify(channel, 'channel')}_{_slugify(date_label, 'all')}.csv"


def _configured_scope_by_key() -> dict[str, dict[str, Any]]:
    return {
        str(scope.get("scope_key")): scope
        for scope in _message_browser_scopes()
        if scope.get("scope_key")
    }


def _bool_arg(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _messages_csv_response(messages: list[Any], filename: str) -> Response:
    output = StringIO()
    output.write("\ufeff")
    writer = csv.DictWriter(output, fieldnames=MESSAGE_EXPORT_COLUMNS)
    writer.writeheader()
    for message in messages:
        writer.writerow(
            {
                "created_at": message.created_at.isoformat() if message.created_at else "",
                "region_name": message.region_name or "",
                "channel_name": message.channel_name or "",
                "message_id": message.message_id,
                "author_id": message.author_id,
                "detected_language": message.detected_language or "unknown",
                "content": message.content or "",
            }
        )

    response = Response(output.getvalue(), content_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _csv_export_response(
    scope_key: str | None,
    report_date: str | None,
    *,
    all_messages: bool = False,
) -> Response | tuple[Response, int]:
    scopes = _configured_scope_by_key()
    if not scopes:
        return jsonify({"error": "No message scopes configured"}), 400
    if not scope_key or scope_key not in scopes:
        return jsonify({"error": "Invalid scope_key"}), 400
    if not all_messages and not report_date:
        return jsonify({"error": "Missing report_date"}), 400

    db = get_db()
    scope = scopes[scope_key]
    if all_messages:
        messages = db.get_messages_for_scope(scope_key=scope_key)
        filename = _message_export_filename(scope, "all", scope_key)
        return _messages_csv_response(messages, filename)

    try:
        selected_date = date.fromisoformat(str(report_date))
    except ValueError:
        return jsonify({"error": "Invalid report_date"}), 400

    window_start, window_end = _local_date_window(selected_date)
    messages = db.get_messages_for_window(
        window_start,
        window_end,
        scope_key=scope_key,
    )
    filename = _message_export_filename(scope, selected_date.strftime("%Y%m%d"), scope_key)
    return _messages_csv_response(messages, filename)


@bp.get("/messages")
def index() -> str:
    return render_template(
        "messages/index.html",
        active_nav="messages",
        page_title="Messages",
    )


@bp.get("/messages/export.csv")
def export_csv() -> Response | tuple[Response, int]:
    return _csv_export_response(
        scope_key=request.args.get("scope_key"),
        report_date=request.args.get("report_date"),
        all_messages=_bool_arg(request.args.get("all_messages")),
    )
