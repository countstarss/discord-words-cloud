from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from ..common import load_config
from ..storage import init_db
from .service import DailyReportService, DailyReportTranslator, LLMBudgetConfig, SHANGHAI_TZ


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
    return DailyReportService(
        db=db,
        translator=translator,
        interval_hours=int(reporting_cfg.get("interval_hours", 2)),
    )


def _run_interval_job(service: DailyReportService) -> None:
    report_date, window_start, window_end = service.build_last_completed_interval_window()
    report = service.generate_hourly_report_for_window(report_date, window_start, window_end)
    print(
        f"[hourly-report] generated report_date={report.report_date} "
        f"window_start={window_start.isoformat()} window_end={window_end.isoformat()} id={report.id}"
    )


def _run_previous_day_daily_job(service: DailyReportService) -> None:
    report_date, window_start, window_end = service.build_previous_day_window()
    report = service.generate_daily_report_from_hourly_reports(report_date)
    print(
        f"[daily-report] generated report_date={report.report_date} "
        f"window_start={window_start.isoformat()} window_end={window_end.isoformat()} id={report.id}"
    )


def run_daily_report_worker(config_path: Optional[str] = None) -> None:
    service = _build_service(config_path)
    local_now = datetime.now(tz=SHANGHAI_TZ)
    created = service.backfill_recent_hourly_reports(now=local_now)
    if created:
        print(f"[hourly-report] startup backfill completed created={created}")

    yesterday = local_now.date() - timedelta(days=1)
    if service.get_daily_report(yesterday) is None and local_now.time() >= dt_time(hour=0, minute=20):
        print(f"[daily-report] backfilling missing report for {yesterday.isoformat()}")
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
    print("[daily-report] worker started timezone=Asia/Shanghai interval=2h@05 daily=00:20")
    scheduler.start()


def run_daily_report_once_now(config_path: Optional[str] = None) -> None:
    service = _build_service(config_path)
    report_date, window_start, window_end = service.build_today_so_far_window()
    report = service.generate_today_so_far_report()
    print(
        f"[daily-report] generated report_date={report.report_date} "
        f"window_start={window_start.isoformat()} window_end={window_end.isoformat()} "
        f"id={report.id} mode=now"
    )
