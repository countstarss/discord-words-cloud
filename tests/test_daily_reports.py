from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from src.reports.delivery import DailyReportFeishuNotifier, FeishuDeliveryConfig
from src.pipeline import build_interval_pipeline_bundle_from_dataframe
from src.reports.service import DailyReportService, ReportScope, SHANGHAI_TZ, render_daily_markdown
from src.storage import Database


class FakeTranslator:
    def build_execution_plan(self, report: dict) -> dict:
        return {
            "remaining_calls": 999,
            "usable_calls": 999,
            "planned_shards": max(1, report.get("shard_count", 1)),
            "shard_char_budget": report.get("shard_char_budget", 12_000),
            "parallel_requests": 1,
            "effective_call_limit": 999,
        }

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
        if "hourly_reports" in summary:
            return "# 每日报告\n\n- 已根据 hourly_reports JSON 生成中文报告。"
        return render_daily_markdown(summary)

    def summarize_channel_text_shard(self, shard: dict, scope: ReportScope, report_date: date) -> str:
        return (
            f"{scope.display_name} 子报告 {report_date.isoformat()}："
            f"候选 {shard['stats']['candidate_count']} 条，"
            f"消息 {shard['stats']['message_count']} 条。"
        )

    def merge_channel_text_reports(
        self,
        *,
        scope: ReportScope,
        report_date: date,
        window_start: datetime,
        window_end: datetime,
        shard_reports: list[str],
        source_message_count: int,
        candidate_message_count: int,
        active_user_count: int,
        max_batch_chars: int = 18_000,
        depth: int = 0,
    ) -> str:
        del window_start, window_end, max_batch_chars, depth
        return (
            f"{scope.display_name} {report_date.isoformat()} 中文日报\n"
            f"总消息 {source_message_count} 条，候选 {candidate_message_count} 条，活跃用户 {active_user_count} 人。\n"
            + "\n".join(shard_reports)
        )


class FlakyTranslator(FakeTranslator):
    def __init__(self) -> None:
        self._failed_once = False

    def summarize_signal_shard(self, shard: dict) -> dict:
        if not self._failed_once and len(shard.get("items", [])) > 1:
            self._failed_once = True
            raise RuntimeError("LLM response did not contain text content")
        return super().summarize_signal_shard(shard)


class CaptureDailyPayloadTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.last_summary: Optional[dict] = None

    def compose_daily_markdown(self, summary: dict) -> str:
        self.last_summary = summary
        return super().compose_daily_markdown(summary)


def _make_db(tmp_path) -> Database:
    db_path = tmp_path / "daily_reports.sqlite3"
    db = Database(f"sqlite+pysqlite:///{db_path}")
    db.init_tables()
    return db


def _message_payload(
    message_id: int,
    content: str,
    created_at: datetime,
    detected_language: str = "th",
    *,
    channel_id: int = 10,
    region_key: str = "default",
    region_name: str = "Default",
    channel_name: str = "channel 10",
    channel_group: str = "chat",
) -> dict:
    return {
        "message_id": message_id,
        "guild_id": 1,
        "channel_id": channel_id,
        "author_id": 100 + message_id,
        "region_key": region_key,
        "region_name": region_name,
        "channel_name": channel_name,
        "channel_group": channel_group,
        "scope_key": f"{region_key}:{channel_id}",
        "content": content,
        "detected_language": detected_language,
        "detected_language_confidence": 0.9,
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


def _daily_report_payload(report_date: date, *, scope_key: str = "global", content_cn: str = "# 每日报告\n\n一切正常。") -> dict:
    return {
        "report_date": report_date,
        "timezone": "Asia/Shanghai",
        "scope_type": "global" if scope_key == "global" else "channel",
        "scope_key": scope_key,
        "region_key": "__all__" if scope_key == "global" else "default",
        "channel_id": 0,
        "channel_name": "",
        "window_start": datetime.combine(report_date, datetime.min.time(), tzinfo=timezone.utc),
        "window_end": datetime.combine(report_date, datetime.min.time(), tzinfo=timezone.utc),
        "content": content_cn,
        "content_cn": content_cn,
        "source_message_count": 12,
        "candidate_message_count": 4,
        "generated_at": datetime.now(tz=timezone.utc),
        "updated_at": datetime.now(tz=timezone.utc),
    }


class FakeFeishuClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_text(self, text: str) -> dict:
        self.messages.append(text)
        return {"code": 0, "msg": "ok"}


def test_hourly_report_upsert_is_idempotent(tmp_path):
    db = _make_db(tmp_path)
    payload = {
        "report_date": date(2026, 3, 24),
        "timezone": "Asia/Shanghai",
        "window_start": datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
        "content_json": {"urgent_issues": []},
        "source_message_count": 10,
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
                "detected_language": "th",
                "quality_score": 1.0,
            },
            {
                "author_id": "2",
                "channel_id": "10",
                "content": "อัปเดตแล้วเปิดไม่ได้ ต้องลงใหม่ทุกครั้ง",
                "created_at": "2026-03-24T00:02:00Z",
                "detected_language": "th",
                "quality_score": 1.0,
            },
            {
                "author_id": "3",
                "channel_id": "10",
                "content": "กดตรงไหนถึงจะเข้า code ai ได้",
                "created_at": "2026-03-24T00:03:00Z",
                "detected_language": "th",
                "quality_score": 1.0,
            },
            {
                "author_id": "4",
                "channel_id": "10",
                "content": "กดตรงไหนถึงจะเข้า code ai ได้",
                "created_at": "2026-03-24T00:04:00Z",
                "detected_language": "th",
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
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=2,
            content="กดตรงไหนถึงจะเข้า code ai ได้",
            created_at=datetime(2026, 3, 23, 18, 10, tzinfo=timezone.utc),
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
    assert stored.candidate_message_count == 2
    assert db.count_hourly_reports(report_date) == 2
    assert "每日报告" in stored.content_cn
    assert "hourly_reports JSON" in stored.content_cn


def test_render_daily_markdown_omits_action_hints(tmp_path):
    del tmp_path
    summary = {
        "report_date": "2026-03-24",
        "timezone": "Asia/Shanghai",
        "window_start": "2026-03-23T16:00:00+00:00",
        "window_end": "2026-03-24T16:00:00+00:00",
        "source_message_count": 12,
        "candidate_message_count": 4,
        "active_user_count": 3,
        "interval_count": 12,
        "shard_count": 2,
        "urgent_issues": [
            {
                "key": "login_failure",
                "category": "bug_report",
                "priority": "high",
                "title": "登录失败",
                "summary": "多名用户反馈更新后无法登录。",
                "message_count": 4,
                "unique_user_count": 3,
                "channel_ids": ["10"],
                "evidence": ["更新后一直卡在登录页"],
                "action_hint": "尽快排查登录链路。",
            }
        ],
        "product_opportunities": [],
        "general_feedback": [],
        "sentiment": {"score": "negative", "reason": "问题反馈集中。"},
        "channel_ids": ["10"],
        "detected_language_breakdown": {"th": 4},
        "filter_details": {"filler": 1},
    }

    markdown = render_daily_markdown(summary)

    assert "建议动作" not in markdown
    assert "尽快排查登录链路" not in markdown


def test_daily_report_llm_payload_omits_action_hints(tmp_path):
    db = _make_db(tmp_path)
    translator = CaptureDailyPayloadTranslator()
    report_date = date(2026, 3, 24)

    db.upsert_message(
        _message_payload(
            message_id=201,
            content="อัปเดตแล้วเข้าไม่ได้ ต้องลงใหม่",
            created_at=datetime(2026, 3, 23, 16, 20, tzinfo=timezone.utc),
        )
    )

    service = DailyReportService(db=db, translator=translator)
    service.generate_hourly_report_for_window(
        report_date,
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
    )

    service.generate_daily_report_from_hourly_reports(report_date)

    assert translator.last_summary is not None
    assert "action_hint" not in json.dumps(translator.last_summary, ensure_ascii=False)


def test_generate_today_so_far_report_uses_today_window(tmp_path):
    db = _make_db(tmp_path)
    now = datetime(2026, 3, 24, 12, 30, tzinfo=SHANGHAI_TZ)

    db.upsert_message(
        _message_payload(
            message_id=1,
            content="เมื่อวานมีบัคแต่วันนี้หายแล้ว",
            created_at=datetime(2026, 3, 23, 15, 59, tzinfo=timezone.utc),
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=2,
            content="วันนี้เข้าไม่ได้หลังอัปเดต",
            created_at=datetime(2026, 3, 24, 1, 0, tzinfo=timezone.utc),
        )
    )

    service = DailyReportService(db=db, translator=FakeTranslator())
    report = service.generate_today_so_far_report(now=now)

    stored = db.get_daily_report_by_date(date(2026, 3, 24))
    assert report.id == stored.id
    assert stored.source_message_count == 1
    assert stored.candidate_message_count == 1
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


def test_channel_scoped_reports_are_generated_separately(tmp_path):
    db = _make_db(tmp_path)
    report_date = date(2026, 3, 24)

    db.upsert_message(
        _message_payload(
            message_id=11,
            content="อัปเดตแล้วส่งข้อความไม่ได้",
            created_at=datetime(2026, 3, 23, 16, 20, tzinfo=timezone.utc),
            channel_id=1400146275512352799,
            region_key="th",
            region_name="泰国",
            channel_name="聊天室",
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=12,
            content="อยากให้เพิ่มประวัติการสนทนา",
            created_at=datetime(2026, 3, 23, 16, 50, tzinfo=timezone.utc),
            channel_id=1400147594625290370,
            region_key="th",
            region_name="泰国",
            channel_name="Rubii反馈",
            channel_group="feedback",
        )
    )

    scopes = [
        ReportScope(scope_type="global", scope_key="global"),
        ReportScope(
            scope_type="channel",
            scope_key="th:1400146275512352799",
            region_key="th",
            region_name="泰国",
            channel_id=1400146275512352799,
            channel_name="聊天室",
        ),
        ReportScope(
            scope_type="channel",
            scope_key="th:1400147594625290370",
            region_key="th",
            region_name="泰国",
            channel_id=1400147594625290370,
            channel_name="Rubii反馈",
        ),
    ]
    service = DailyReportService(db=db, translator=FakeTranslator(), scopes=scopes)
    reports = service.generate_hourly_reports_for_window(
        report_date,
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
    )

    assert len(reports) == 3
    global_report = db.get_hourly_report_by_window(
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
        scope_key="global",
    )
    chat_report = db.get_hourly_report_by_window(
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
        scope_key="th:1400146275512352799",
    )
    feedback_report = db.get_hourly_report_by_window(
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
        scope_key="th:1400147594625290370",
    )

    assert global_report is not None and global_report.source_message_count == 2
    assert chat_report is not None and chat_report.source_message_count == 1
    assert feedback_report is not None and feedback_report.source_message_count == 1
    assert feedback_report.channel_name == "Rubii反馈"


def test_hourly_report_survives_flaky_shard_response(tmp_path):
    db = _make_db(tmp_path)
    report_date = date(2026, 3, 24)
    db.upsert_message(
        _message_payload(
            message_id=31,
            content="อัปเดตแล้วเข้าไม่ได้ ต้องลงใหม่",
            created_at=datetime(2026, 3, 23, 16, 10, tzinfo=timezone.utc),
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=32,
            content="กดตรงไหนถึงจะเข้า code ai ได้",
            created_at=datetime(2026, 3, 23, 16, 20, tzinfo=timezone.utc),
        )
    )

    service = DailyReportService(db=db, translator=FlakyTranslator())
    report = service.generate_hourly_report_for_window(
        report_date,
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
    )

    assert report.source_message_count == 2
    assert report.candidate_message_count == 2


def test_generate_channel_today_text_report(tmp_path):
    db = _make_db(tmp_path)
    now = datetime(2026, 3, 24, 12, 30, tzinfo=SHANGHAI_TZ)
    channel_id = 1400146275512352799
    region_key = "th"

    db.upsert_message(
        _message_payload(
            message_id=41,
            content="อัปเดตแล้วเข้าไม่ได้ ต้องลงใหม่",
            created_at=datetime(2026, 3, 24, 1, 10, tzinfo=timezone.utc),
            channel_id=channel_id,
            region_key=region_key,
            region_name="泰国",
            channel_name="聊天室",
        )
    )
    db.upsert_message(
        _message_payload(
            message_id=42,
            content="กดตรงไหนถึงจะเข้า code ai ได้",
            created_at=datetime(2026, 3, 24, 2, 0, tzinfo=timezone.utc),
            channel_id=channel_id,
            region_key=region_key,
            region_name="泰国",
            channel_name="聊天室",
        )
    )

    scopes = [
        ReportScope(scope_type="global", scope_key="global"),
        ReportScope(
            scope_type="channel",
            scope_key=f"{region_key}:{channel_id}",
            region_key=region_key,
            region_name="泰国",
            channel_id=channel_id,
            channel_name="聊天室",
        ),
    ]
    service = DailyReportService(db=db, translator=FakeTranslator(), scopes=scopes)
    report = service.generate_channel_today_text_report(channel_id=channel_id, now=now)

    assert report.scope_key == f"{region_key}:{channel_id}"
    assert report.source_message_count == 2
    assert report.candidate_message_count == 2
    assert "泰国 / 聊天室" in report.content_cn


def test_feishu_notifier_sends_report_once(tmp_path):
    db = _make_db(tmp_path)
    report_date = date(2026, 3, 24)
    db.upsert_daily_report(
        _daily_report_payload(
            report_date,
            content_cn="# Global 日报\n\n今天有 4 个高优先级问题需要跟进。",
        )
    )

    service = DailyReportService(db=db, translator=FakeTranslator())
    client = FakeFeishuClient()
    notifier = DailyReportFeishuNotifier(
        db=db,
        config=FeishuDeliveryConfig(
            enabled=True,
            webhook_url="https://open.feishu.test/hook/123",
            keyword="日报",
            title_prefix="Global 日报",
        ),
        client=client,
    )

    first = notifier.deliver_previous_day_report(
        service,
        now=datetime(2026, 3, 25, 9, 0, tzinfo=SHANGHAI_TZ),
    )
    second = notifier.deliver_previous_day_report(
        service,
        now=datetime(2026, 3, 25, 9, 10, tzinfo=SHANGHAI_TZ),
    )

    assert first.status == "sent"
    assert first.sent_message_count == 1
    assert second.status == "already_delivered"
    assert len(client.messages) == 1
    assert client.messages[0].startswith("日报 Global 日报 2026-03-24")


def test_feishu_notifier_generates_missing_daily_report_from_hourly_reports(tmp_path):
    db = _make_db(tmp_path)
    report_date = date(2026, 3, 24)

    db.upsert_message(
        _message_payload(
            message_id=101,
            content="อัปเดตแล้วเข้าไม่ได้ ต้องลงใหม่",
            created_at=datetime(2026, 3, 23, 16, 10, tzinfo=timezone.utc),
        )
    )
    service = DailyReportService(db=db, translator=FakeTranslator())
    service.generate_hourly_report_for_window(
        report_date,
        datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
    )

    client = FakeFeishuClient()
    notifier = DailyReportFeishuNotifier(
        db=db,
        config=FeishuDeliveryConfig(
            enabled=True,
            webhook_url="https://open.feishu.test/hook/456",
            title_prefix="Global 日报",
        ),
        client=client,
    )

    result = notifier.deliver_report_for_date(service, report_date=report_date)
    stored = db.get_daily_report_by_date(report_date)

    assert result.status == "sent"
    assert stored is not None
    assert stored.content_cn
    assert len(client.messages) == 1


def test_feishu_notifier_splits_long_report_into_multiple_messages(tmp_path):
    db = _make_db(tmp_path)
    report_date = date(2026, 3, 24)
    long_body = "# Global 日报\n\n" + ("这是很长的日报内容。\n" * 120)
    db.upsert_daily_report(_daily_report_payload(report_date, content_cn=long_body))

    service = DailyReportService(db=db, translator=FakeTranslator())
    client = FakeFeishuClient()
    notifier = DailyReportFeishuNotifier(
        db=db,
        config=FeishuDeliveryConfig(
            enabled=True,
            webhook_url="https://open.feishu.test/hook/789",
            keyword="日报",
            title_prefix="Global 日报",
            max_message_chars=320,
        ),
        client=client,
    )

    result = notifier.deliver_report_for_date(service, report_date=report_date)

    assert result.status == "sent"
    assert len(client.messages) >= 2
    assert all(message.startswith("日报 Global 日报 2026-03-24") for message in client.messages)


def test_run_previous_day_feishu_delivery_now_prints_sent_status(monkeypatch, capsys, tmp_path):
    from src.reports.delivery import FeishuDeliveryResult
    from src.reports.worker import run_previous_day_feishu_delivery_now

    class StubNotifier:
        def is_enabled(self) -> bool:
            return True

        def deliver_previous_day_report(self, service):
            del service
            return FeishuDeliveryResult(
                status="sent",
                report_date=date(2026, 3, 24),
                scope_key="global",
                sent_message_count=1,
                report_found=True,
            )

    monkeypatch.setattr(
        "src.reports.worker._build_runtime",
        lambda config_path=None: (object(), StubNotifier()),
    )

    run_previous_day_feishu_delivery_now(config_path=str(tmp_path / "config.yaml"))
    captured = capsys.readouterr()
    assert "[feishu] sent date=2026-03-24 scope=global messages=1" in captured.out


def test_build_translator_can_be_optional_when_llm_env_missing(monkeypatch):
    from src.reports.worker import _build_translator

    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    translator = _build_translator({}, required=False)

    assert translator is None
