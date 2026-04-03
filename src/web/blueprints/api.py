from __future__ import annotations

from flask import Blueprint, request

from ...api.app import (
    get_daily_reports as api_get_daily_reports,
    get_dashboard as api_get_dashboard,
    get_message as api_get_message,
    get_messages as api_get_messages,
    get_stats as api_get_stats,
    health as api_health,
    status as api_status,
)
from ..markdown import enrich_daily_report_payload

bp = Blueprint("web_api", __name__)


def _int_arg(name: str, default: int) -> int:
    value = request.args.get(name, type=int)
    return default if value is None else value


@bp.get("/api/health")
def health() -> dict:
    return api_health()


@bp.get("/api/status")
def status() -> dict:
    hours = _int_arg("hours", 24)
    return api_status(hours=hours)


@bp.get("/api/messages")
def messages() -> dict:
    limit = _int_arg("limit", 100)
    offset = _int_arg("offset", 0)
    scope_key = request.args.get("scope_key")
    return api_get_messages(limit=limit, offset=offset, scope_key=scope_key)


@bp.get("/api/messages/<int:message_id>")
def message(message_id: int) -> dict:
    return api_get_message(message_id)


@bp.get("/api/stats")
def stats() -> dict:
    hours = _int_arg("hours", 24)
    scope_key = request.args.get("scope_key")
    return api_get_stats(hours=hours, scope_key=scope_key)


@bp.get("/api/dashboard")
def dashboard_payload() -> dict:
    return api_get_dashboard()


@bp.get("/daily-report")
def daily_report() -> dict:
    scope_key = request.args.get("scope_key", "global")
    payload = api_get_daily_reports(scope_key=scope_key)
    return enrich_daily_report_payload(payload)
