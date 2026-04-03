from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from ..common import load_config
from ..storage import init_db
from .service import (
    DailyReportService,
    DailyReportTranslator,
    LLMBudgetConfig,
    SHANGHAI_TZ,
    build_report_scopes,
)


def _database_url_from_config(config: dict) -> Optional[str]:
    db_cfg = config.get("database", {})
    if db_cfg.get("url"):
        return db_cfg["url"]

    host = db_cfg.get("host")
    port = db_cfg.get("port")
    name = db_cfg.get("name")
    user = db_cfg.get("user")
    password = db_cfg.get("password")
    if not all([host, port, name, user]):
        return None
    password = password or ""
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def _build_service(config_path: Optional[str] = None) -> DailyReportService:
    config = load_config(config_path)
    db = init_db(database_url=_database_url_from_config(config))
    reporting_cfg = config.get("reporting", {})
    budget_config = LLMBudgetConfig.from_config(reporting_cfg.get("llm"))
    translator = DailyReportTranslator.from_env(budget_config=budget_config)
    scopes = build_report_scopes(config.get("targets"))
    return DailyReportService(
        db=db,
        translator=translator,
        interval_hours=int(reporting_cfg.get("interval_hours", 2)),
        scopes=scopes,
    )


def _run_interval_job(service: DailyReportService) -> None:
    try:
        report_date, window_start, window_end = service.build_last_completed_interval_window()
        reports = service.generate_hourly_reports_for_window(report_date, window_start, window_end)
        active = [report for report in reports if int(report.source_message_count or 0) > 0]
        print(
            f"[hourly] {window_start.astimezone(SHANGHAI_TZ).strftime('%H:%M')}~{window_end.astimezone(SHANGHAI_TZ).strftime('%H:%M')} "
            f"channels={len(reports)} active={len(active)} msgs={sum(int(report.source_message_count or 0) for report in reports)}"
        )
        for report in active:
            print(
                f"  [ok] {report.region_key}/{report.channel_name} msgs={report.source_message_count} "
                f"candidates={report.candidate_message_count} shards={report.shard_count}"
            )
    except Exception as exc:
        print(f"[hourly] failed reason={type(exc).__name__}: {exc}")


def _run_previous_day_daily_job(service: DailyReportService) -> None:
    try:
        report_date, window_start, window_end = service.build_previous_day_window()
        reports = service.generate_previous_day_reports()
        active = [report for report in reports if int(report.source_message_count or 0) > 0]
        print(
            f"[daily] {report_date.isoformat()} regions={len(reports)} active={len(active)} "
            f"window={window_start.astimezone(SHANGHAI_TZ).strftime('%m-%d %H:%M')}~{window_end.astimezone(SHANGHAI_TZ).strftime('%m-%d %H:%M')}"
        )
        for report in active:
            print(f"  [ok] {report.region_key} ({report.channel_name}) msgs={report.source_message_count} candidates={report.candidate_message_count}")
    except Exception as exc:
        print(f"[daily] failed reason={type(exc).__name__}: {exc}")


def run_daily_report_worker(config_path: Optional[str] = None) -> None:
    service = _build_service(config_path)
    local_now = datetime.now(tz=SHANGHAI_TZ)

    hourly_scopes = service.iter_hourly_scopes()
    daily_scopes = service.iter_daily_scopes()
    region_names = [scope.region_name for scope in daily_scopes]

    yesterday = local_now.date() - timedelta(days=1)
    missing_daily_scope = any(service.get_daily_report(yesterday, scope_key=scope.scope_key) is None for scope in daily_scopes)
    if missing_daily_scope and local_now.time() >= dt_time(hour=0, minute=20):
        print(f"[worker] backfill daily {yesterday.isoformat()}")
        _run_previous_day_daily_job(service)

    scheduler = BlockingScheduler(timezone=SHANGHAI_TZ)
    scheduler.add_job(
        _run_interval_job,
        CronTrigger(hour="0,2,4,6,8,10,12,14,16,18,20,22", minute=5, timezone=SHANGHAI_TZ),
        args=[service],
        id="hourly-report-2h",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_previous_day_daily_job,
        CronTrigger(hour=0, minute=20, timezone=SHANGHAI_TZ),
        args=[service],
        id="daily-report-merge",
        replace_existing=True,
    )
    print(
        f"[worker] ready tz=Asia/Shanghai interval=2h@05 daily=00:20 "
        f"channels={len(hourly_scopes)} regions={len(daily_scopes)} ({', '.join(region_names)}) "
        f"parallel={service.translator.budget_config.max_parallel_requests if service.translator else 1}"
    )
    scheduler.start()


def run_daily_report_for_date(date_value: str, config_path: Optional[str] = None) -> None:
    service = _build_service(config_path)
    report_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    reports = service.generate_daily_reports_for_date(report_date)
    active = [report for report in reports if int(report.source_message_count or 0) > 0]
    print(f"[daily] manual date={report_date.isoformat()} regions={len(reports)} active={len(active)}")
    for report in active:
        print(f"  [ok] {report.region_key} ({report.channel_name}) msgs={report.source_message_count} candidates={report.candidate_message_count}")
