from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.pipeline import run_pipeline_from_dataframe
from src.reports.service import DailyReportService, SHANGHAI_TZ
from src.storage import Database


class FakeTranslator:
    def translate_markdown_to_chinese(self, markdown: str) -> str:
        return f"# 中文版\n\n{markdown}"


def _make_db(tmp_path) -> Database:
    db_path = tmp_path / "daily_reports.sqlite3"
    db = Database(f"sqlite+pysqlite:///{db_path}")
    db.init_tables()
    return db


def _message_payload(message_id: int, content: str, created_at: datetime, is_target_language: bool) -> dict:
    return {
        "message_id": message_id,
        "guild_id": 1,
        "channel_id": 10,
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


def test_daily_report_upsert_is_idempotent(tmp_path):
    db = _make_db(tmp_path)
    payload = {
        "report_date": date(2026, 3, 24),
        "timezone": "Asia/Shanghai",
        "window_start": datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 24, 16, 0, tzinfo=timezone.utc),
        "content": "# thai",
        "content_cn": "# chinese",
        "source_message_count": 10,
        "target_message_count": 7,
        "generated_at": datetime(2026, 3, 24, 16, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 3, 24, 16, 1, tzinfo=timezone.utc),
    }

    first = db.upsert_daily_report(payload)
    payload["content_cn"] = "# chinese updated"
    second = db.upsert_daily_report(payload)

    reports = db.get_all_daily_reports()
    assert first.id == second.id
    assert len(reports) == 1
    assert reports[0].content_cn == "# chinese updated"


def test_pipeline_counts_all_messages_but_clusters_only_target_language():
    df = pd.DataFrame(
        [
            {
                "author_id": "1",
                "content": "แอปล่ม เปิดไม่ได้เลย",
                "created_at": "2026-03-24T01:00:00Z",
                "is_target_language": True,
                "quality_score": 1.0,
            },
            {
                "author_id": "2",
                "content": "general english chat",
                "created_at": "2026-03-24T02:00:00Z",
                "is_target_language": False,
                "quality_score": 1.0,
            },
        ]
    )

    result = run_pipeline_from_dataframe(
        df=df,
        report_date="2026-03-24",
        timezone_name="Asia/Shanghai",
        window_start=datetime(2026, 3, 24, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 24, 23, 59, tzinfo=timezone.utc),
    )

    report = result["report"]
    assert report["source_message_count"] == 2
    assert report["target_message_count"] == 1
    assert report["total_clusters"] >= 1
    assert "รายงานประจำวัน" in result["markdown"]


def test_pipeline_filters_longform_story_text_from_report():
    df = pd.DataFrame(
        [
            {
                "author_id": "1",
                "content": '"มารีน่า" นักศึกษาคณะวิทยาศาสตร์ทางทะเลปี 2 ผู้มีนิสัยลุยๆ ปากแจ๋วแต่ลึกๆ แอบขี้กลัว '
                'นึกคึกอยากลองดีท้าพิสูจน์ตำนาน Bloody Mary หลังเลิกเรียนที่มหาลัยในโลกที่ทุกคนต่างมีความลับ '
                'เนื้อเรื่องนี้เริ่มจากเพื่อนสนิทในโรงเรียนที่ต้องเผชิญชะตากรรมร่วมกันและค่อยๆ เปิดเผยปมในอดีต',
                "created_at": "2026-03-24T01:00:00Z",
                "is_target_language": True,
                "quality_score": 1.0,
            },
            {
                "author_id": "2",
                "content": "อัปเดตแล้วเปิดไม่ได้ ต้องลบแอพติดตั้งใหม่ทุกครั้ง ช่วยดูให้หน่อย",
                "created_at": "2026-03-24T02:00:00Z",
                "is_target_language": True,
                "quality_score": 1.0,
            },
        ]
    )

    result = run_pipeline_from_dataframe(
        df=df,
        report_date="2026-03-24",
        timezone_name="Asia/Shanghai",
        window_start=datetime(2026, 3, 24, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 24, 23, 59, tzinfo=timezone.utc),
    )

    report = result["report"]
    assert report["source_message_count"] == 2
    assert report["target_message_count"] == 2
    assert report["pipeline_stats"]["filter_details"].get("longform_story", 0) == 1
    assert report["total_clusters"] >= 1
    assert "มารีน่า" not in result["markdown"]


def test_daily_report_service_generates_today_so_far_report(tmp_path):
    db = _make_db(tmp_path)
    now = datetime(2026, 3, 24, 12, 30, tzinfo=SHANGHAI_TZ)

    db.upsert_message(
        _message_payload(
            message_id=1,
            content="อัปเดตแล้วเปิดไม่ได้",
            created_at=datetime(2026, 3, 24, 1, 0, tzinfo=timezone.utc),
            is_target_language=True,
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=2,
            content="misc note",
            created_at=datetime(2026, 3, 24, 2, 0, tzinfo=timezone.utc),
            is_target_language=False,
        )
    )

    service = DailyReportService(db=db, translator=FakeTranslator())
    report = service.generate_today_so_far_report(now=now)

    stored = db.get_daily_report_by_date(date(2026, 3, 24))
    assert report.id == stored.id
    assert stored.source_message_count == 2
    assert stored.target_message_count == 1
    assert stored.content.startswith("# รายงานประจำวัน")
    assert stored.content_cn.startswith("# 中文版")


def test_previous_day_window_uses_shanghai_calendar():
    service = DailyReportService(db=Database("sqlite+pysqlite:///:memory:"), translator=FakeTranslator())
    report_date, window_start, window_end = service.build_previous_day_window(
        now=datetime(2026, 3, 25, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert report_date == date(2026, 3, 24)
    assert window_start == datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc)
    assert window_end == datetime(2026, 3, 24, 16, 0, tzinfo=timezone.utc)
