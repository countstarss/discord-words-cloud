from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from ..common import database_url_from_config, load_config
from ..storage import init_db
from .delivery import DailyReportFeishuNotifier, FeishuDeliveryConfig
from .service import (
    DailyReportService,
    DailyReportTranslator,
    LLMBudgetConfig,
    SHANGHAI_TZ,
    build_report_scopes,
)


def _build_translator(reporting_cfg: dict, *, required: bool) -> Optional[DailyReportTranslator]:
    budget_config = LLMBudgetConfig.from_config(reporting_cfg.get("llm"))
    if required:
        return DailyReportTranslator.from_env(budget_config=budget_config)
    try:
        return DailyReportTranslator.from_env(budget_config=budget_config)
    except RuntimeError:
        return None


def _build_runtime(
    config_path: Optional[str] = None,
    *,
    require_translator: bool = True,
) -> tuple[DailyReportService, DailyReportFeishuNotifier]:
    config = load_config(config_path)
    db = init_db(database_url=database_url_from_config(config))
    reporting_cfg = config.get("reporting", {})
    translator = _build_translator(reporting_cfg, required=require_translator)
    scopes = build_report_scopes(config.get("targets"))
    service = DailyReportService(
        db=db,
        translator=translator,
        interval_hours=int(reporting_cfg.get("interval_hours", 2)),
        scopes=scopes,
    )
    notifier = DailyReportFeishuNotifier(
        db=db,
        config=FeishuDeliveryConfig.from_config(((reporting_cfg.get("delivery") or {}).get("feishu"))),
    )
    return service, notifier


def _run_interval_job(service: DailyReportService) -> None:
    try:
        report_date, window_start, window_end = service.build_last_completed_interval_window()
        reports = service.generate_hourly_reports_for_window(report_date, window_start, window_end)
        active = [report for report in reports if int(report.source_message_count or 0) > 0]
        print(
            f"[hourly] {window_start.astimezone(SHANGHAI_TZ).strftime('%H:%M')}~{window_end.astimezone(SHANGHAI_TZ).strftime('%H:%M')} "
            f"scopes={len(reports)} active={len(active)} msgs={sum(int(report.source_message_count or 0) for report in reports)}"
        )
        for report in active:
            label = "全部频道" if report.scope_type == "global" else f"{report.region_key}/{report.channel_name}"
            print(
                f"  [ok] {label} msgs={report.source_message_count} "
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
            f"[daily] {report_date.isoformat()} scopes={len(reports)} active={len(active)} "
            f"window={window_start.astimezone(SHANGHAI_TZ).strftime('%m-%d %H:%M')}~{window_end.astimezone(SHANGHAI_TZ).strftime('%m-%d %H:%M')}"
        )
        for report in active:
            label = "全部频道" if report.scope_type == "global" else f"{report.region_key}/{report.channel_name}"
            print(f"  [ok] {label} msgs={report.source_message_count} candidates={report.candidate_message_count}")
    except Exception as exc:
        print(f"[daily] failed reason={type(exc).__name__}: {exc}")


def _run_previous_day_feishu_job(service: DailyReportService, notifier: DailyReportFeishuNotifier) -> None:
    if not notifier.is_enabled():
        print("[feishu] skipped reason=disabled")
        return
    try:
        result = notifier.deliver_previous_day_report(service)
        if result.status == "already_delivered":
            print(f"[feishu] skip date={result.report_date.isoformat()} scope={result.scope_key} reason=already_delivered")
            return
        if result.status == "disabled":
            print("[feishu] skipped reason=disabled")
            return
        print(
            f"[feishu] sent date={result.report_date.isoformat()} scope={result.scope_key} "
            f"messages={result.sent_message_count}"
        )
    except Exception as exc:
        print(f"[feishu] failed reason={type(exc).__name__}: {exc}")


def run_daily_report_worker(config_path: Optional[str] = None) -> None:
    service, notifier = _build_runtime(config_path)
    local_now = datetime.now(tz=SHANGHAI_TZ)

    yesterday = local_now.date() - timedelta(days=1)
    missing_daily_scope = any(service.get_daily_report(yesterday, scope_key=scope.scope_key) is None for scope in service.scopes)
    if missing_daily_scope and local_now.time() >= dt_time(hour=0, minute=20):
        print(f"[worker] backfill daily {yesterday.isoformat()}")
        _run_previous_day_daily_job(service)
    if notifier.is_enabled() and local_now.time() >= dt_time(
        hour=notifier.config.daily_send_hour,
        minute=notifier.config.daily_send_minute,
    ):
        print(f"[worker] backfill feishu {yesterday.isoformat()}")
        _run_previous_day_feishu_job(service, notifier)

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
    if notifier.is_enabled():
        scheduler.add_job(
            _run_previous_day_feishu_job,
            CronTrigger(
                hour=notifier.config.daily_send_hour,
                minute=notifier.config.daily_send_minute,
                timezone=SHANGHAI_TZ,
            ),
            args=[service, notifier],
            id="daily-report-feishu",
            replace_existing=True,
        )
    print(
        f"[worker] ready tz=Asia/Shanghai interval=2h@05 daily=00:20 scopes={len(service.scopes)} "
        f"parallel={service.translator.budget_config.max_parallel_requests if service.translator else 1} "
        f"feishu={'on' if notifier.is_enabled() else 'off'}"
    )
    scheduler.start()


def run_previous_day_feishu_delivery_now(config_path: Optional[str] = None) -> None:
    service, notifier = _build_runtime(config_path, require_translator=False)
    if not notifier.is_enabled():
        raise RuntimeError("Feishu delivery is not enabled. Set FEISHU_BOT_ENABLED=true and FEISHU_BOT_WEBHOOK_URL.")

    result = notifier.deliver_previous_day_report(service)
    if result.status == "already_delivered":
        print(f"[feishu] skip date={result.report_date.isoformat()} scope={result.scope_key} reason=already_delivered")
        return
    if result.status == "disabled":
        raise RuntimeError("Feishu delivery is disabled.")

    print(
        f"[feishu] sent date={result.report_date.isoformat()} scope={result.scope_key} "
        f"messages={result.sent_message_count}"
    )


def run_daily_report_once_now(config_path: Optional[str] = None) -> None:
    service, _ = _build_runtime(config_path)
    report_date, window_start, window_end = service.build_today_so_far_window()
    report = service.generate_today_so_far_report()
    print(
        f"[preview] 全部频道 date={report.report_date} "
        f"window={window_start.astimezone(SHANGHAI_TZ).strftime('%H:%M')}~{window_end.astimezone(SHANGHAI_TZ).strftime('%H:%M')} "
        f"msgs={report.source_message_count} candidates={report.candidate_message_count}"
    )


def run_daily_report_for_date(date_value: str, config_path: Optional[str] = None) -> None:
    service, _ = _build_runtime(config_path)
    report_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    reports = service.generate_daily_reports_for_date(report_date)
    active = [report for report in reports if int(report.source_message_count or 0) > 0]
    print(f"[daily] manual date={report_date.isoformat()} scopes={len(reports)} active={len(active)}")
    for report in active:
        label = "全部频道" if report.scope_type == "global" else f"{report.region_key}/{report.channel_name}"
        print(f"  [ok] {label} msgs={report.source_message_count} candidates={report.candidate_message_count}")


def run_channel_daily_report_worker(channel_id: int, config_path: Optional[str] = None) -> None:
    service, _ = _build_runtime(config_path)
    scope = service.get_scope_for_channel(channel_id)
    report_date, window_start, window_end = service.build_today_so_far_window()
    report = service.generate_channel_today_text_report(channel_id=channel_id)
    print(
        f"[channel-daily] {scope.display_name} date={report_date.isoformat()} "
        f"window={window_start.astimezone(SHANGHAI_TZ).strftime('%H:%M')}~{window_end.astimezone(SHANGHAI_TZ).strftime('%H:%M')} "
        f"msgs={report.source_message_count} candidates={report.candidate_message_count}"
    )
