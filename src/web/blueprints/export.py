from __future__ import annotations

import json
import re
from datetime import date
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Blueprint, Response, jsonify, render_template, request, send_file

from ...common import load_config
from ...storage import DailyReport, HourlyReport, get_db

bp = Blueprint("export", __name__)


def _configured_export_scopes() -> list[dict[str, Any]]:
    config = load_config()
    targets = config.get("targets", {})
    scopes = [{"scope_key": "__all__", "label": "全部 scope", "scope_type": "all"}]
    scopes.append({"scope_key": "global", "label": "全部频道 / Global", "scope_type": "global"})
    for region in targets.get("regions", []) or []:
        region_key = str(region.get("key") or "").strip() or "__all__"
        region_name = str(region.get("name") or region_key or "Region").strip()
        for channel in region.get("channels", []) or []:
            channel_id = int(channel.get("id"))
            channel_name = str(channel.get("name") or f"channel {channel_id}").strip()
            scopes.append(
                {
                    "scope_key": f"{region_key}:{channel_id}",
                    "label": f"{region_name} / {channel_name}",
                    "scope_type": "channel",
                    "region_key": region_key,
                    "region_name": region_name,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                }
            )
    return scopes


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "report"


def _scope_label(scope_key: str, scopes: list[dict[str, Any]]) -> str:
    for scope in scopes:
        if scope.get("scope_key") == scope_key:
            return str(scope.get("label") or scope_key)
    return scope_key


def _daily_item_payload(report: DailyReport, scopes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "export_key": f"daily:{report.id}",
        "report_type": "daily",
        "id": int(report.id),
        "scope_key": report.scope_key,
        "scope_label": _scope_label(report.scope_key, scopes),
        "title": f"日报 · {report.report_date.isoformat()}",
        "subtitle": report.channel_name or report.region_key or report.scope_key,
        "window_label": f"{report.window_start.isoformat()} -> {report.window_end.isoformat()}",
        "source_message_count": int(report.source_message_count or 0),
        "candidate_message_count": int(report.candidate_message_count or 0),
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
    }


def _hourly_item_payload(report: HourlyReport, scopes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "export_key": f"hourly:{report.id}",
        "report_type": "hourly",
        "id": int(report.id),
        "scope_key": report.scope_key,
        "scope_label": _scope_label(report.scope_key, scopes),
        "title": f"小时报告 · {report.window_start.strftime('%H:%M')} - {report.window_end.strftime('%H:%M')}",
        "subtitle": report.channel_name or report.region_key or report.scope_key,
        "window_label": f"{report.window_start.isoformat()} -> {report.window_end.isoformat()}",
        "source_message_count": int(report.source_message_count or 0),
        "candidate_message_count": int(report.candidate_message_count or 0),
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
    }


def _catalog_payload(report_type: str, scope_key: str | None, report_date: str | None) -> dict[str, Any]:
    db = get_db()
    scopes = _configured_export_scopes()
    selected_scope = scope_key or "__all__"
    selected_scope_value = None if selected_scope == "__all__" else selected_scope

    if report_type == "hourly":
        available_dates = [
            item.isoformat()
            for item in db.get_hourly_report_dates(scope_key=selected_scope_value, non_empty_only=True)
        ]
    else:
        report_type = "daily"
        available_dates = [
            item.isoformat()
            for item in db.get_daily_report_dates(scope_key=selected_scope_value, non_empty_only=True)
        ]

    if report_date:
        try:
            selected_date = date.fromisoformat(report_date)
        except ValueError:
            selected_date = date.fromisoformat(available_dates[0]) if available_dates else None
    else:
        selected_date = date.fromisoformat(available_dates[0]) if available_dates else None

    items: list[dict[str, Any]] = []
    if selected_date is not None:
        if report_type == "hourly":
            reports = db.list_hourly_reports(
                report_date=selected_date,
                scope_key=selected_scope_value,
                non_empty_only=True,
            )
            items = [_hourly_item_payload(report, scopes) for report in reports]
        else:
            reports = db.list_daily_reports(
                report_date=selected_date,
                scope_key=selected_scope_value,
                non_empty_only=True,
            )
            items = [_daily_item_payload(report, scopes) for report in reports]

    return {
        "report_type": report_type,
        "available_scopes": scopes,
        "available_dates": available_dates,
        "selected_scope": selected_scope,
        "selected_date": selected_date.isoformat() if selected_date else None,
        "items": items,
    }


def _daily_export_bytes(report: DailyReport, scopes: list[dict[str, Any]]) -> tuple[str, bytes]:
    scope_label = _scope_label(report.scope_key, scopes)
    header = "\n".join(
        [
            f"# Daily Report · {report.report_date.isoformat()}",
            "",
            f"- Scope: {scope_label}",
            f"- Window: {report.window_start.isoformat()} -> {report.window_end.isoformat()}",
            f"- Messages: {int(report.source_message_count or 0)}",
            f"- Candidates: {int(report.candidate_message_count or 0)}",
            "",
            "---",
            "",
        ]
    )
    payload = f"{header}{report.content_cn or ''}".encode("utf-8")
    base_name = f"daily_{report.report_date.isoformat()}_{_slugify(scope_label)}.md"
    return base_name, payload


def _hourly_export_bytes(report: HourlyReport, scopes: list[dict[str, Any]]) -> tuple[str, bytes]:
    scope_label = _scope_label(report.scope_key, scopes)
    payload = {
        "report_type": "hourly",
        "report_date": report.report_date.isoformat(),
        "scope_key": report.scope_key,
        "scope_label": scope_label,
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "source_message_count": int(report.source_message_count or 0),
        "candidate_message_count": int(report.candidate_message_count or 0),
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "content_json": report.content_json or {},
    }
    window_label = report.window_start.strftime("%H%M") + "-" + report.window_end.strftime("%H%M")
    base_name = f"hourly_{report.report_date.isoformat()}_{window_label}_{_slugify(scope_label)}.json"
    return base_name, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _download_response(items: list[dict[str, Any]]) -> Response:
    normalized_items = [item for item in items if isinstance(item, dict)]
    daily_ids = [int(item["id"]) for item in normalized_items if item.get("report_type") == "daily" and item.get("id")]
    hourly_ids = [int(item["id"]) for item in normalized_items if item.get("report_type") == "hourly" and item.get("id")]

    db = get_db()
    scopes = _configured_export_scopes()
    daily_map = {int(report.id): _daily_export_bytes(report, scopes) for report in db.get_daily_reports_by_ids(daily_ids)}
    hourly_map = {int(report.id): _hourly_export_bytes(report, scopes) for report in db.get_hourly_reports_by_ids(hourly_ids)}
    files: list[tuple[str, bytes]] = []
    for item in normalized_items:
        report_id = item.get("id")
        if not report_id:
            continue
        if item.get("report_type") == "daily" and int(report_id) in daily_map:
            files.append(daily_map[int(report_id)])
        elif item.get("report_type") == "hourly" and int(report_id) in hourly_map:
            files.append(hourly_map[int(report_id)])

    if not files:
        return jsonify({"error": "No reports selected"}), 400

    if len(files) == 1:
        filename, content = files[0]
        mimetype = "application/json" if filename.endswith(".json") else "text/markdown; charset=utf-8"
        return send_file(
            BytesIO(content),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
        )

    archive = BytesIO()
    manifest = []
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        for filename, content in files:
            zip_file.writestr(filename, content)
            manifest.append({"filename": filename, "bytes": len(content)})
        zip_file.writestr("manifest.json", json.dumps({"items": manifest}, ensure_ascii=False, indent=2))
    archive.seek(0)
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name="reports-export.zip",
    )


@bp.get("/export")
def index() -> str:
    return render_template(
        "export/index.html",
        active_nav="export",
        page_title="Export",
    )


@bp.get("/api/export/catalog")
def catalog() -> Response:
    report_type = str(request.args.get("report_type") or "daily").strip().lower()
    scope_key = request.args.get("scope_key")
    report_date = request.args.get("report_date")
    return jsonify(_catalog_payload(report_type, scope_key, report_date))


@bp.post("/export/download")
def download() -> Response:
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    return _download_response(items)
