from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.pipeline import build_interval_pipeline_bundle_from_dataframe
from src.reports.service import DailyReportService, SHANGHAI_TZ, render_daily_markdown
from src.storage import Database


class FakeTranslator:
    def summarize_signal_shard(self, shard: dict) -> dict:
        urgent_issues = []
        product_opportunities = []
        general_feedback = []
        for item in shard["items"]:
            content = item["content"]
            hints = set(item.get("hint_categories", []))
            entry = {
                "key": item["candidate_id"].replace("-", "_"),
                "category": next(iter(hints), "other"),
                "priority": "medium",
                "title": content[:18],
                "summary": f"围绕“{content[:12]}”形成了集中讨论。",
                "message_count": item["message_count"],
                "unique_user_count": item["unique_user_count"],
                "channel_ids": [item["channel_id"]],
                "evidence": [content[:40]],
                "action_hint": "复核对应功能链路。",
            }
            if {"bug_report", "update_issue", "performance", "ai_quality"} & hints:
                entry["priority"] = "high"
                urgent_issues.append(entry)
            elif {"feature_request", "ux_confusion"} & hints or item.get("question"):
                product_opportunities.append(entry)
            else:
                general_feedback.append(entry)

        return {
            "urgent_issues": urgent_issues[:8],
            "product_opportunities": product_opportunities[:6],
            "general_feedback": general_feedback[:4],
            "sentiment": {"score": "negative" if urgent_issues else "neutral", "reason": "测试桩输出"},
        }

    def compose_daily_markdown(self, summary: dict) -> str:
        return render_daily_markdown(summary)


def _make_db(tmp_path) -> Database:
    db_path = tmp_path / "daily_reports.sqlite3"
    db = Database(f"sqlite+pysqlite:///{db_path}")
    db.init_tables()
    return db


def _message_payload(
    message_id: int,
    content: str,
    created_at: datetime,
    is_target_language: bool,
    *,
    channel_id: int = 10,
) -> dict:
    return {
        "message_id": message_id,
        "guild_id": 1,
        "channel_id": channel_id,
        "author_id": 100 + message_id,
        "content": content,
        "is_target_language": is_target_language,
        "language": "th" if is_target_language else "other",
        "lang_confidence": 0.9 if is_target_language else 0.1,
        "cleaned_text": content,
        "tokens": [],
        "event_type": "create",
        "is_deleted": False,
        "content_hash": f"hash-{message_id}",
        "is_duplicate": False,
        "quality_score": 1.0,
        "created_at": created_at,
        "updated_at": created_at,
        "deleted_at": None,
    }


def test_hourly_report_upsert_is_idempotent(tmp_path):
    db = _make_db(tmp_path)
    payload = {
        "report_date": date(2026, 3, 24),
        "timezone": "Asia/Shanghai",
        "window_start": datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
        "content_json": {"urgent_issues": []},
        "source_message_count": 10,
        "target_message_count": 7,
        "candidate_message_count": 6,
        "shard_count": 1,
        "generated_at": datetime(2026, 3, 23, 18, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 3, 23, 18, 1, tzinfo=timezone.utc),
    }

    first = db.upsert_hourly_report(payload)
    payload["candidate_message_count"] = 8
    second = db.upsert_hourly_report(payload)

    reports = db.get_hourly_reports_for_date(date(2026, 3, 24))
    assert first.id == second.id
    assert len(reports) == 1
    assert reports[0].candidate_message_count == 8


def test_pipeline_filters_noise_and_shards_payload():
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "author_id": "1",
                "channel_id": "10",
                "content": "55",
                "created_at": "2026-03-24T00:01:00Z",
                "is_target_language": True,
                "quality_score": 1.0,
            },
            {
                "author_id": "2",
                "channel_id": "10",
                "content": "อัปเดตแล้วเปิดไม่ได้ ต้องลงใหม่ทุกครั้ง",
                "created_at": "2026-03-24T00:02:00Z",
                "is_target_language": True,
                "quality_score": 1.0,
            },
            {
                "author_id": "3",
                "channel_id": "10",
                "content": "กดตรงไหนถึงจะเข้า code ai ได้",
                "created_at": "2026-03-24T00:03:00Z",
                "is_target_language": True,
                "quality_score": 1.0,
            },
            {
                "author_id": "4",
                "channel_id": "10",
                "content": "กดตรงไหนถึงจะเข้า code ai ได้",
                "created_at": "2026-03-24T00:04:00Z",
                "is_target_language": True,
                "quality_score": 1.0,
            },
        ]
    )

    result = build_interval_pipeline_bundle_from_dataframe(
        df=df,
        report_date="2026-03-24",
        timezone_name="Asia/Shanghai",
        window_start=datetime(2026, 3, 24, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 24, 2, 0, tzinfo=timezone.utc),
        shard_char_budget=220,
        shard_max_items=1,
    )

    report = result["report"]
    assert report["source_message_count"] == 4
    assert report["candidate_message_count"] == 3
    assert report["candidate_group_count"] == 2
    assert report["filter_details"]["filler"] == 1
    assert report["shard_count"] == 2
    assert result["candidates"][0]["signal_score"] >= result["candidates"][1]["signal_score"]


def test_daily_report_service_generates_hourly_and_daily_reports(tmp_path):
    db = _make_db(tmp_path)
    report_date = date(2026, 3, 24)

    db.upsert_message(
        _message_payload(
            message_id=1,
            content="อัปเดตแล้วเปิดไม่ได้ ต้องลบแอพติดตั้งใหม่",
            created_at=datetime(2026, 3, 23, 16, 30, tzinfo=timezone.utc),
            is_target_language=True,
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=2,
            content="กดตรงไหนถึงจะเข้า code ai ได้",
            created_at=datetime(2026, 3, 23, 18, 10, tzinfo=timezone.utc),
            is_target_language=True,
        )
    )

    service = DailyReportService(db=db, translator=FakeTranslator())
    service.generate_hourly_report_for_window(
        report_date,
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
    )
    service.generate_hourly_report_for_window(
        report_date,
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 20, 0, tzinfo=timezone.utc),
    )

    report = service.generate_daily_report_from_hourly_reports(report_date)
    stored = db.get_daily_report_by_date(report_date)

    assert report.id == stored.id
    assert stored.source_message_count == 2
    assert stored.target_message_count == 2
    assert db.count_hourly_reports(report_date) == 12
    assert "每日报告" in stored.content_cn
    assert "重点问题" in stored.content_cn
    assert "产品机会" in stored.content_cn


def test_generate_today_so_far_report_uses_today_window(tmp_path):
    db = _make_db(tmp_path)
    now = datetime(2026, 3, 24, 12, 30, tzinfo=SHANGHAI_TZ)

    db.upsert_message(
        _message_payload(
            message_id=1,
            content="เมื่อวานมีบัคแต่วันนี้หายแล้ว",
            created_at=datetime(2026, 3, 23, 15, 59, tzinfo=timezone.utc),
            is_target_language=True,
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=2,
            content="วันนี้เข้าไม่ได้หลังอัปเดต",
            created_at=datetime(2026, 3, 24, 1, 0, tzinfo=timezone.utc),
            is_target_language=True,
        )
    )

    service = DailyReportService(db=db, translator=FakeTranslator())
    report = service.generate_today_so_far_report(now=now)

    stored = db.get_daily_report_by_date(date(2026, 3, 24))
    assert report.id == stored.id
    assert stored.source_message_count == 1
    assert stored.target_message_count == 1
    assert stored.window_start.replace(tzinfo=timezone.utc) == datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc)
    assert stored.window_end.replace(tzinfo=timezone.utc) == now.astimezone(timezone.utc)


def test_previous_day_and_interval_windows_use_shanghai_calendar():
    service = DailyReportService(db=Database("sqlite+pysqlite:///:memory:"), translator=FakeTranslator())

    report_date, window_start, window_end = service.build_previous_day_window(
        now=datetime(2026, 3, 25, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    interval_date, interval_start, interval_end = service.build_last_completed_interval_window(
        now=datetime(2026, 3, 24, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert report_date == date(2026, 3, 24)
    assert window_start == datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc)
    assert window_end == datetime(2026, 3, 24, 16, 0, tzinfo=timezone.utc)
    assert interval_date == date(2026, 3, 24)
    assert interval_start == datetime(2026, 3, 24, 0, 0, tzinfo=timezone.utc)
    assert interval_end == datetime(2026, 3, 24, 2, 0, tzinfo=timezone.utc)
