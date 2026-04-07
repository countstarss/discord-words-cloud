from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..common import load_config
from ..storage import get_db


def _configured_regions() -> list[dict]:
    config = load_config()
    targets = config.get("targets", {})
    regions = []
    for region in targets.get("regions", []) or []:
        channels = []
        for channel in region.get("channels", []) or []:
            channels.append(
                {
                    "id": int(channel.get("id")),
                    "name": str(channel.get("name") or f"channel {channel.get('id')}").strip(),
                    "group": str(channel.get("group") or "").strip(),
                    "scope_key": f"{str(region.get('key') or '').strip() or '__all__'}:{int(channel.get('id'))}",
                    "guild_ids": [int(item) for item in channel.get("guild_ids", []) or []],
                }
            )
        regions.append(
            {
                "key": str(region.get("key") or ""),
                "name": str(region.get("name") or region.get("key") or "Region").strip(),
                "guild_ids": [int(item) for item in region.get("guild_ids", []) or []],
                "channels": channels,
            }
        )
    return regions


def _message_browser_scopes() -> list[dict]:
    return [
        {
            "scope_key": channel["scope_key"],
            "label": f"{region['name']} / {channel['name']}",
            "region_key": region["key"],
            "region_name": region["name"],
            "channel_id": channel["id"],
            "channel_name": channel["name"],
        }
        for region in _configured_regions()
        for channel in region.get("channels", [])
    ]


def _local_date_window(report_date: date, timezone_name: str = "Asia/Shanghai") -> tuple[datetime, datetime]:
    local_tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(report_date, time.min, tzinfo=local_tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _serialize_message(message, *, include_cleaned_text: bool = True) -> dict:
    payload = {
        "message_id": message.message_id,
        "guild_id": message.guild_id,
        "channel_id": message.channel_id,
        "author_id": message.author_id,
        "region_key": message.region_key,
        "region_name": message.region_name,
        "channel_name": message.channel_name,
        "channel_group": message.channel_group,
        "scope_key": message.scope_key,
        "content": message.content,
        "detected_language": message.detected_language,
        "detected_language_confidence": message.detected_language_confidence,
        "quality_score": message.quality_score,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }
    if include_cleaned_text:
        payload["cleaned_text"] = message.cleaned_text
    return payload


def health() -> dict:
    return {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()}


def status(hours: int = 24) -> dict:
    db = get_db()
    return db.get_stats(hours=hours)


def get_messages(
    limit: int = 100,
    offset: int = 0,
    scope_key: Optional[str] = None,
) -> dict:
    db = get_db()
    messages = db.get_all_messages(
        limit=limit,
        offset=offset,
        scope_key=scope_key,
    )
    return {
        "messages": [_serialize_message(message) for message in messages],
        "limit": limit,
        "offset": offset,
    }


def get_messages_browser(
    scope_key: Optional[str] = None,
    report_date: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    db = get_db()
    scopes = _message_browser_scopes()
    selected_scope = scope_key or (scopes[0]["scope_key"] if scopes else None)
    if not selected_scope:
        return {
            "available_scopes": [],
            "available_dates": [],
            "selected_scope": None,
            "selected_date": None,
            "messages": [],
            "pagination": {"page": 1, "page_size": page_size, "total_items": 0, "total_pages": 0},
        }

    available_dates = [item.isoformat() for item in db.get_message_dates_for_scope(scope_key=selected_scope)]
    if report_date:
        try:
            selected_date = date.fromisoformat(report_date)
        except ValueError:
            selected_date = date.fromisoformat(available_dates[0]) if available_dates else date.today()
    else:
        selected_date = date.fromisoformat(available_dates[0]) if available_dates else date.today()

    window_start, window_end = _local_date_window(selected_date)
    total_items = db.count_messages_for_window(window_start, window_end, scope_key=selected_scope)
    total_pages = max(1, (total_items + page_size - 1) // page_size) if total_items else 0
    current_page = min(page, total_pages or 1)
    offset = (current_page - 1) * page_size
    messages = db.get_messages_page_for_window(
        window_start,
        window_end,
        scope_key=selected_scope,
        limit=page_size,
        offset=offset,
    )

    return {
        "available_scopes": scopes,
        "available_dates": available_dates,
        "selected_scope": selected_scope,
        "selected_date": selected_date.isoformat(),
        "messages": [_serialize_message(message, include_cleaned_text=False) for message in messages],
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
        },
    }


def get_message(message_id: int) -> dict:
    db = get_db()
    message = db.get_message_by_id(message_id)
    if message is None:
        return {"error": "Message not found"}
    payload = _serialize_message(message)
    payload.update(
        {
            "tokens": message.tokens,
            "updated_at": message.updated_at.isoformat() if message.updated_at else None,
        }
    )
    return payload


def get_stats(hours: int = 24, scope_key: Optional[str] = None) -> dict:
    db = get_db()
    del scope_key
    return db.get_stats(hours=hours)


def get_dashboard() -> dict:
    db = get_db()
    stats = db.get_stats(hours=24)
    reports = db.get_all_daily_reports()
    latest_report = reports[0] if reports else None
    hourly_count_today = db.count_hourly_reports(date.today())
    regions = _configured_regions()
    return {
        "total_messages": db.count_messages(),
        "total_reports": len(reports),
        "total_hourly_reports_today": hourly_count_today,
        "active_users_24h": stats.get("active_users", 0),
        "detected_language_breakdown_24h": stats.get("detected_language_breakdown", []),
        "latest_message_at": stats.get("last_message_at"),
        "latest_report_date": latest_report.report_date.isoformat() if latest_report else None,
        "configured_channel_count": sum(len(region.get("channels", [])) for region in regions),
        "configured_regions": regions,
        "available_scopes": [{"scope_key": "global", "scope_type": "global", "label": "全部频道"}]
        + [
            {
                "scope_key": channel["scope_key"],
                "scope_type": "channel",
                "label": f"{region['name']} / {channel['name']}",
                "region_key": region["key"],
                "region_name": region["name"],
                "channel_id": channel["id"],
                "channel_name": channel["name"],
            }
            for region in regions
            for channel in region.get("channels", [])
        ],
    }


def get_daily_reports(scope_key: str = "global") -> dict:
    db = get_db()
    reports = db.get_all_daily_reports(scope_key=scope_key)
    return {
        "reports": [
            {
                "report_date": report.report_date.isoformat(),
                "timezone": report.timezone,
                "scope_type": report.scope_type,
                "scope_key": report.scope_key,
                "region_key": report.region_key,
                "channel_id": report.channel_id,
                "channel_name": report.channel_name,
                "window_start": report.window_start.isoformat(),
                "window_end": report.window_end.isoformat(),
                "generated_at": report.generated_at.isoformat(),
                "source_message_count": report.source_message_count,
                "candidate_message_count": report.candidate_message_count,
                "content_cn": report.content_cn,
            }
            for report in reports
        ]
    }
