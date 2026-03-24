from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from ..pipeline import build_daily_report_from_messages
from ..storage import Database


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _compose_empty_chinese_markdown(report_date: date, window_start: datetime, window_end: datetime) -> str:
    return "\n".join(
        [
            "# 每日报告",
            "",
            f"- 报告日期: {report_date.isoformat()}",
            "- 时区: Asia/Shanghai",
            f"- 数据窗口: {window_start.isoformat()} 至 {window_end.isoformat()}",
            "",
            "## 今日概览",
            "",
            "- 今日没有新的已收集消息。",
            "",
            "## 关键问题",
            "",
            "- 无",
            "",
            "## 产品机会",
            "",
            "- 暂无足够数据。",
            "",
            "## 用户情绪",
            "",
            "- 中性，因为当天没有新增消息。",
            "",
            "## 代表性讨论摘录",
            "",
            "- 无",
            "",
            "## 数据说明",
            "",
            "- 总消息数: 0",
            "- 目标语言消息数: 0",
        ]
    )


@dataclass
class DailyReportTranslator:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "DailyReportTranslator":
        base_url = os.getenv("LLM_BASE_URL", "").strip()
        model = os.getenv("LLM_MODEL", "").strip()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if not base_url or not model or not api_key:
            raise RuntimeError("Missing LLM_BASE_URL, LLM_MODEL, or LLM_API_KEY for daily report translation")
        return cls(base_url=base_url, model=model, api_key=api_key)

    def _messages_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _request_timeout(self, markdown: str) -> httpx.Timeout:
        # Large daily reports can easily exceed 30s on Anthropic-compatible gateways.
        total_timeout = max(self.timeout_seconds, min(180.0, 30.0 + (len(markdown) / 250.0)))
        return httpx.Timeout(total_timeout, connect=20.0)

    def translate_markdown_to_chinese(self, markdown: str) -> str:
        timeout = self._request_timeout(markdown)
        payload = {
            "model": self.model,
            "max_tokens": 8192,
            "temperature": 0,
            "system": (
                "You are a faithful technical translator for product analytics reports. "
                "Translate Thai markdown into Simplified Chinese markdown. "
                "Preserve headings, bullets, emphasis, dates, and factual meaning. "
                "Do not add conclusions or remove content."
            ),
            "messages": [{"role": "user", "content": markdown}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        for attempt in range(1, 4):
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(self._messages_url(), headers=headers, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                body = response.json()
                chunks = body.get("content", [])
                text_parts = [chunk.get("text", "") for chunk in chunks if chunk.get("type") == "text"]
                translated = "\n".join(part for part in text_parts if part.strip()).strip()
                if not translated:
                    raise RuntimeError("LLM translation response did not contain text content")
                return translated
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if attempt < 3 and (status == 429 or status >= 500):
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise RuntimeError(f"Daily report translation failed with HTTP {status}") from exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise RuntimeError(
                    f"Daily report translation failed after retries (timeout={timeout.read}s, chars={len(markdown)})"
                ) from exc


class DailyReportService:
    def __init__(
        self,
        db: Database,
        translator: Optional[DailyReportTranslator] = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.db = db
        self.translator = translator
        self.timezone_name = timezone_name
        self.local_tz = ZoneInfo(timezone_name)

    def get_daily_report(self, report_date: date):
        return self.db.get_daily_report_by_date(report_date)

    def _local_midnight(self, target_date: date) -> datetime:
        return datetime.combine(target_date, dt_time.min, tzinfo=self.local_tz)

    def build_previous_day_window(self, now: Optional[datetime] = None) -> tuple[date, datetime, datetime]:
        local_now = (now or datetime.now(tz=self.local_tz)).astimezone(self.local_tz)
        report_date = local_now.date() - timedelta(days=1)
        local_start = self._local_midnight(report_date)
        local_end = local_start + timedelta(days=1)
        return report_date, local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)

    def build_today_so_far_window(self, now: Optional[datetime] = None) -> tuple[date, datetime, datetime]:
        local_now = (now or datetime.now(tz=self.local_tz)).astimezone(self.local_tz)
        report_date = local_now.date()
        local_start = self._local_midnight(report_date)
        return report_date, local_start.astimezone(timezone.utc), local_now.astimezone(timezone.utc)

    def generate_for_window(self, report_date: date, window_start: datetime, window_end: datetime):
        utc_start = _ensure_utc(window_start)
        utc_end = _ensure_utc(window_end)
        messages = self.db.get_messages_for_window(utc_start, utc_end)
        pipeline_result = build_daily_report_from_messages(
            messages=messages,
            report_date=report_date,
            timezone_name=self.timezone_name,
            window_start=utc_start,
            window_end=utc_end,
        )
        report = pipeline_result["report"]
        content = pipeline_result["markdown"]

        if report["source_message_count"] == 0:
            content_cn = _compose_empty_chinese_markdown(report_date, utc_start, utc_end)
        else:
            if self.translator is None:
                raise RuntimeError("Daily report translator is required for non-empty reports")
            content_cn = self.translator.translate_markdown_to_chinese(content)

        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "window_start": utc_start,
            "window_end": utc_end,
            "content": content,
            "content_cn": content_cn,
            "source_message_count": report["source_message_count"],
            "target_message_count": report["target_message_count"],
            "generated_at": datetime.now(tz=timezone.utc),
            "updated_at": datetime.now(tz=timezone.utc),
        }
        return self.db.upsert_daily_report(payload)

    def generate_previous_day_report(self, now: Optional[datetime] = None):
        report_date, window_start, window_end = self.build_previous_day_window(now=now)
        return self.generate_for_window(report_date, window_start, window_end)

    def generate_today_so_far_report(self, now: Optional[datetime] = None):
        report_date, window_start, window_end = self.build_today_so_far_window(now=now)
        return self.generate_for_window(report_date, window_start, window_end)
