from __future__ import annotations

from datetime import datetime
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from ..common import load_config
from ..storage import init_db
from .service import DailyReportService, DailyReportTranslator, SHANGHAI_TZ


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
    translator = DailyReportTranslator.from_env()
    return DailyReportService(db=db, translator=translator)


def _run_previous_day_job(service: DailyReportService) -> None:
    report_date, window_start, window_end = service.build_previous_day_window()
    report = service.generate_for_window(report_date, window_start, window_end)
    print(
        f"[daily-report] generated report_date={report.report_date} "
        f"window_start={window_start.isoformat()} window_end={window_end.isoformat()} id={report.id}"
    )


def run_daily_report_worker(config_path: Optional[str] = None) -> None:
    service = _build_service(config_path)
    report_date, _, _ = service.build_previous_day_window(now=datetime.now(tz=SHANGHAI_TZ))
    if service.get_daily_report(report_date) is None:
        print(f"[daily-report] backfilling missing report for {report_date.isoformat()}")
        _run_previous_day_job(service)

    scheduler = BlockingScheduler(timezone=SHANGHAI_TZ)
    scheduler.add_job(
        _run_previous_day_job,
        CronTrigger(hour=0, minute=0, timezone=SHANGHAI_TZ),
        args=[service],
        id="daily-report-midnight",
        replace_existing=True,
    )
    print("[daily-report] worker started timezone=Asia/Shanghai schedule=00:00")
    scheduler.start()


def run_daily_report_once_now(config_path: Optional[str] = None) -> None:
    service = _build_service(config_path)
    report_date, window_start, window_end = service.build_today_so_far_window()
    report = service.generate_for_window(report_date, window_start, window_end)
    print(
        f"[daily-report] generated report_date={report.report_date} "
        f"window_start={window_start.isoformat()} window_end={window_end.isoformat()} "
        f"id={report.id} mode=now"
    )
