from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from ..pipeline import build_interval_pipeline_bundle_from_messages
from ..storage import Database


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
SECTION_LIMITS = {
    "urgent_issues": 12,
    "product_opportunities": 10,
    "general_feedback": 8,
}


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_key(category: str, payload: dict[str, Any]) -> str:
    raw_key = str(payload.get("key") or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_]{3,80}", raw_key):
        return raw_key
    title = str(payload.get("title") or "").strip()
    evidence = "|".join(str(item) for item in payload.get("evidence", [])[:2])
    seed = f"{category}|{title}|{evidence}"
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()[:10]
    return f"{category}_{digest}"


def _priority_value(priority: str) -> int:
    return PRIORITY_RANK.get(str(priority or "medium").lower(), 1)


def _normalize_sentiment(value: dict[str, Any] | None) -> dict[str, str]:
    score = str((value or {}).get("score") or "neutral").lower()
    if score not in {"negative", "neutral", "positive"}:
        score = "neutral"
    reason = str((value or {}).get("reason") or "").strip()
    return {"score": score, "reason": reason}


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", candidate):
        start = match.start()
        try:
            parsed, _ = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError(f"LLM response did not contain a decodable JSON object: {candidate[:400]}")


def _coerce_string_list(values: Any, limit: int = 5) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _normalize_entry(section: str, payload: Any, fallback_index: int) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    category = str(payload.get("category") or "other").strip().lower()
    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if not title and not summary:
        return None
    priority = str(payload.get("priority") or ("medium" if section != "general_feedback" else "low")).strip().lower()
    if priority not in PRIORITY_RANK:
        priority = "medium"
    entry = {
        "key": _stable_key(category, payload) if category else f"item_{fallback_index}",
        "category": category or "other",
        "priority": priority,
        "title": title or summary[:32],
        "summary": summary or title,
        "message_count": max(0, int(payload.get("message_count") or 0)),
        "unique_user_count": max(0, int(payload.get("unique_user_count") or 0)),
        "channel_ids": _coerce_string_list(payload.get("channel_ids"), limit=10),
        "evidence": _coerce_string_list(payload.get("evidence"), limit=4),
        "action_hint": str(payload.get("action_hint") or "").strip(),
    }
    return entry


def _merge_section_items(summaries: list[dict[str, Any]], section: str) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    fallback_index = 0
    for summary in summaries:
        raw_items = summary.get(section, [])
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            fallback_index += 1
            item = _normalize_entry(section, raw_item, fallback_index=fallback_index)
            if item is None:
                continue
            key = (item["category"], item["key"])
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
                continue

            existing["message_count"] += item["message_count"]
            existing["unique_user_count"] += item["unique_user_count"]
            existing["channel_ids"] = _coerce_string_list(existing["channel_ids"] + item["channel_ids"], limit=10)
            existing["evidence"] = _coerce_string_list(existing["evidence"] + item["evidence"], limit=5)
            if _priority_value(item["priority"]) < _priority_value(existing["priority"]):
                existing["priority"] = item["priority"]
            if len(item["summary"]) > len(existing["summary"]):
                existing["summary"] = item["summary"]
            if not existing["action_hint"] and item["action_hint"]:
                existing["action_hint"] = item["action_hint"]

    ordered = sorted(
        merged.values(),
        key=lambda item: (
            _priority_value(item["priority"]),
            -int(item["message_count"]),
            -int(item["unique_user_count"]),
            item["title"],
        ),
    )
    return ordered[: SECTION_LIMITS[section]]


def _derive_sentiment(summary: dict[str, Any]) -> dict[str, str]:
    urgent_weight = sum(
        item.get("message_count", 0) * (3 if item.get("priority") == "high" else 2)
        for item in summary.get("urgent_issues", [])
    )
    opportunity_weight = sum(item.get("message_count", 0) for item in summary.get("product_opportunities", []))
    general_weight = sum(item.get("message_count", 0) for item in summary.get("general_feedback", []))
    if urgent_weight == 0 and opportunity_weight == 0 and general_weight == 0:
        return {"score": "neutral", "reason": "当天没有足够强的产品信号。"}
    if urgent_weight >= max(6, opportunity_weight * 2):
        return {"score": "negative", "reason": "高优先级问题明显多于功能机会，用户情绪偏负面。"}
    if opportunity_weight > urgent_weight and urgent_weight <= 3:
        return {"score": "neutral", "reason": "以产品机会和使用反馈为主，负面问题不集中。"}
    return {"score": "neutral", "reason": "问题与建议并存，整体情绪中性偏谨慎。"}


def _empty_daily_markdown(report_date: date, window_start: datetime, window_end: datetime) -> str:
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
            "## 重点问题",
            "",
            "- 无",
            "",
            "## 产品机会",
            "",
            "- 暂无足够数据。",
            "",
            "## 一般反馈",
            "",
            "- 无。",
            "",
            "## 用户情绪",
            "",
            "- 中性，因为当天没有新增消息。",
            "",
            "## 数据说明",
            "",
            "- 总消息数: 0",
            "- 目标语言消息数: 0",
            "- 候选信号消息数: 0",
        ]
    )


def render_daily_markdown(summary: dict[str, Any]) -> str:
    report_date = summary["report_date"]
    lines = [
        "# 每日报告",
        "",
        f"- 报告日期: {report_date}",
        f"- 时区: {summary['timezone']}",
        f"- 数据窗口: {summary['window_start']} 至 {summary['window_end']}",
        "",
        "## 今日概览",
        "",
        f"- 收集消息总数: {summary['source_message_count']}",
        f"- 目标语言消息数: {summary['target_message_count']}",
        f"- 进入分析的候选消息数: {summary['candidate_message_count']}",
        f"- 活跃用户数: {summary['active_user_count']}",
        f"- 2 小时间隔报告数: {summary.get('interval_count', 1)}",
        f"- LLM 分片数: {summary.get('shard_count', 0)}",
        "",
        "## 重点问题",
        "",
    ]

    def _render_entries(entries: list[dict[str, Any]], empty_text: str) -> None:
        if not entries:
            lines.append(empty_text)
            lines.append("")
            return
        for entry in entries:
            lines.append(f"### {entry['title']}")
            lines.append(
                f"- 分类: {entry['category']} | 优先级: {entry['priority']} | 影响: {entry['message_count']} 条消息 / {entry['unique_user_count']} 位用户（估算）"
            )
            lines.append(f"- 摘要: {entry['summary']}")
            if entry["evidence"]:
                lines.append("- 证据:")
                for item in entry["evidence"]:
                    lines.append(f"  - {item}")
            if entry["action_hint"]:
                lines.append(f"- 建议动作: {entry['action_hint']}")
            lines.append("")

    _render_entries(summary.get("urgent_issues", []), " - 未发现集中的高优先级问题。")
    lines.extend(["## 产品机会", ""])
    _render_entries(summary.get("product_opportunities", []), "- 暂无明确的高价值产品机会。")
    lines.extend(["## 一般反馈", ""])
    _render_entries(summary.get("general_feedback", []), "- 未发现需要单独记录的一般反馈。")

    sentiment = summary.get("sentiment", {"score": "neutral", "reason": "无足够数据。"})
    lines.extend(
        [
            "## 用户情绪",
            "",
            f"- 情绪判断: {sentiment.get('score', 'neutral')}",
            f"- 原因: {sentiment.get('reason', '')}",
            "",
            "## 数据说明",
            "",
            f"- 覆盖频道数: {len(summary.get('channel_ids', []))}",
            f"- 过滤详情: {json.dumps(summary.get('filter_details', {}), ensure_ascii=False)}",
        ]
    )
    return "\n".join(lines)


def build_empty_summary(
    *,
    report_date: date,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    return {
        "report_date": report_date.isoformat(),
        "timezone": timezone_name,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "source_message_count": 0,
        "target_message_count": 0,
        "candidate_message_count": 0,
        "active_user_count": 0,
        "channel_ids": [],
        "shard_count": 0,
        "interval_count": 0,
        "filter_details": {},
        "urgent_issues": [],
        "product_opportunities": [],
        "general_feedback": [],
        "sentiment": {"score": "neutral", "reason": "当天没有新的已收集消息。"},
    }


@dataclass
class DailyReportTranslator:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 45.0

    @classmethod
    def from_env(cls) -> "DailyReportTranslator":
        base_url = os.getenv("LLM_BASE_URL", "").strip()
        model = os.getenv("LLM_MODEL", "").strip()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if not base_url or not model or not api_key:
            raise RuntimeError("Missing LLM_BASE_URL, LLM_MODEL, or LLM_API_KEY for report generation")
        return cls(base_url=base_url, model=model, api_key=api_key)

    def _messages_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _request_timeout(self, payload_chars: int) -> httpx.Timeout:
        total_timeout = max(self.timeout_seconds, min(180.0, 35.0 + (payload_chars / 220.0)))
        return httpx.Timeout(total_timeout, connect=20.0)

    def _request_text(self, *, system: str, user_content: str, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        timeout = self._request_timeout(len(user_content) + len(system))

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
                text = "\n".join(part for part in text_parts if part.strip()).strip()
                if not text:
                    raise RuntimeError("LLM response did not contain text content")
                return text
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if attempt < 3 and (status == 429 or status >= 500):
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise RuntimeError(f"LLM request failed with HTTP {status}") from exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise RuntimeError(
                    f"LLM request failed after retries (timeout={timeout.read}s, chars={len(user_content)})"
                ) from exc

    def summarize_signal_shard(self, shard: dict[str, Any]) -> dict[str, Any]:
        system = (
            "You are a product intelligence analyst. "
            "Summarize batched Discord feedback into a strict JSON object only. "
            "Keep the most important issues, opportunities, and general feedback. "
            "Ignore small talk unless it clearly contains product-relevant signal. "
            "Never output markdown, never wrap the JSON in commentary."
        )
        user_content = (
            "Return a JSON object with exactly these top-level keys:\n"
            "{\n"
            '  "urgent_issues": [...],\n'
            '  "product_opportunities": [...],\n'
            '  "general_feedback": [...],\n'
            '  "sentiment": {"score": "negative|neutral|positive", "reason": "short Chinese sentence"}\n'
            "}\n\n"
            "Rules:\n"
            '- each item must contain: "key", "category", "priority", "title", "summary", '
            '"message_count", "unique_user_count", "channel_ids", "evidence", "action_hint"\n'
            '- "key" must be english_snake_case and stable\n'
            '- "priority" must be one of high, medium, low\n'
            '- "title" and "summary" should be concise Simplified Chinese\n'
            '- "evidence" must be 1-3 short original-language excerpts when available\n'
            '- urgent_issues max 8 items, product_opportunities max 6 items, general_feedback max 4 items\n'
            '- use only facts supported by the input; merge semantically similar complaints inside the shard\n'
            "- if there is no meaningful product signal, return empty arrays and neutral sentiment\n\n"
            f"Shard payload:\n{json.dumps(shard, ensure_ascii=False)}"
        )
        text = self._request_text(system=system, user_content=user_content, max_tokens=4096)
        payload = _extract_json_object(text)
        return {
            "urgent_issues": payload.get("urgent_issues", []),
            "product_opportunities": payload.get("product_opportunities", []),
            "general_feedback": payload.get("general_feedback", []),
            "sentiment": _normalize_sentiment(payload.get("sentiment")),
        }

    def compose_daily_markdown(self, summary: dict[str, Any]) -> str:
        system = (
            "You write concise high-signal daily product reports in Simplified Chinese markdown. "
            "Input is structured JSON merged from multiple 2-hour reports. "
            "Use only facts in the JSON. Do not invent details. Preserve specificity."
        )
        user_content = (
            "Write a markdown report with sections exactly in this order:\n"
            "# 每日报告\n"
            "## 今日概览\n"
            "## 重点问题\n"
            "## 产品机会\n"
            "## 一般反馈\n"
            "## 用户情绪\n"
            "## 数据说明\n\n"
            "Requirements:\n"
            "- keep it concise but specific\n"
            "- rank the most important issues first\n"
            "- mention message counts and affected users where useful\n"
            "- include short evidence bullets under major issues when present\n"
            "- output markdown only\n\n"
            f"Structured JSON:\n{json.dumps(summary, ensure_ascii=False)}"
        )
        return self._request_text(system=system, user_content=user_content, max_tokens=4096).strip()


class DailyReportService:
    def __init__(
        self,
        db: Database,
        translator: Optional[DailyReportTranslator] = None,
        timezone_name: str = "Asia/Shanghai",
        interval_hours: int = 2,
    ) -> None:
        self.db = db
        self.translator = translator
        self.timezone_name = timezone_name
        self.local_tz = ZoneInfo(timezone_name)
        self.interval_hours = interval_hours

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

    def build_last_completed_interval_window(self, now: Optional[datetime] = None) -> tuple[date, datetime, datetime]:
        local_now = (now or datetime.now(tz=self.local_tz)).astimezone(self.local_tz)
        current_hour = local_now.replace(minute=0, second=0, microsecond=0)
        completed_hour = (current_hour.hour // self.interval_hours) * self.interval_hours
        local_end = current_hour.replace(hour=completed_hour)
        local_start = local_end - timedelta(hours=self.interval_hours)
        report_date = local_start.date()
        return report_date, local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)

    def iter_interval_windows_for_date(self, report_date: date) -> list[tuple[date, datetime, datetime]]:
        local_start = self._local_midnight(report_date)
        windows = []
        for hour in range(0, 24, self.interval_hours):
            start = local_start + timedelta(hours=hour)
            end = start + timedelta(hours=self.interval_hours)
            windows.append((report_date, start.astimezone(timezone.utc), end.astimezone(timezone.utc)))
        return windows

    def _build_summary_from_messages(
        self,
        *,
        report_date: date,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        utc_start = _ensure_utc(window_start)
        utc_end = _ensure_utc(window_end)
        messages = self.db.get_messages_for_window(utc_start, utc_end)
        if not messages:
            return build_empty_summary(
                report_date=report_date,
                timezone_name=self.timezone_name,
                window_start=utc_start,
                window_end=utc_end,
            )

        bundle = build_interval_pipeline_bundle_from_messages(
            messages=messages,
            report_date=report_date,
            timezone_name=self.timezone_name,
            window_start=utc_start,
            window_end=utc_end,
        )
        report = bundle["report"]
        base_summary = {
            "report_date": report["report_date"],
            "timezone": report["timezone"],
            "window_start": report["window_start"],
            "window_end": report["window_end"],
            "source_message_count": report["source_message_count"],
            "target_message_count": report["target_message_count"],
            "candidate_message_count": report["candidate_message_count"],
            "active_user_count": report["active_user_count"],
            "channel_ids": sorted({str(getattr(msg, "channel_id", "")) for msg in messages if getattr(msg, "channel_id", None)}),
            "shard_count": report["shard_count"],
            "interval_count": 1,
            "filter_details": report["filter_details"],
        }
        if report["candidate_message_count"] == 0:
            summary = dict(base_summary)
            summary.update(
                {
                    "urgent_issues": [],
                    "product_opportunities": [],
                    "general_feedback": [],
                    "sentiment": {"score": "neutral", "reason": "当天消息存在，但没有足够强的产品信号。"},
                }
            )
            return summary

        if self.translator is None:
            raise RuntimeError("LLM translator is required for non-empty report generation")

        shard_summaries = []
        total_shards = len(bundle["shards"])
        if total_shards > 1:
            print(
                f"[llm-shard] processing {total_shards} shards "
                f"candidates={report['candidate_group_count']} raw_candidates={report.get('raw_candidate_group_count', report['candidate_group_count'])}"
            )
        for index, shard in enumerate(bundle["shards"], start=1):
            if total_shards > 1:
                print(
                    f"[llm-shard] {index}/{total_shards} "
                    f"items={len(shard['items'])} messages={shard['stats'].get('message_count', 0)}"
                )
            shard_summaries.append(self.translator.summarize_signal_shard(shard))
        summary = dict(base_summary)
        summary["urgent_issues"] = _merge_section_items(shard_summaries, "urgent_issues")
        summary["product_opportunities"] = _merge_section_items(shard_summaries, "product_opportunities")
        summary["general_feedback"] = _merge_section_items(shard_summaries, "general_feedback")
        summary["sentiment"] = _derive_sentiment(summary)
        return summary

    def generate_hourly_report_for_window(self, report_date: date, window_start: datetime, window_end: datetime):
        summary = self._build_summary_from_messages(
            report_date=report_date,
            window_start=window_start,
            window_end=window_end,
        )
        now = datetime.now(tz=timezone.utc)
        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "window_start": _ensure_utc(window_start),
            "window_end": _ensure_utc(window_end),
            "content_json": summary,
            "source_message_count": summary["source_message_count"],
            "target_message_count": summary["target_message_count"],
            "candidate_message_count": summary["candidate_message_count"],
            "shard_count": summary.get("shard_count", 0),
            "generated_at": now,
            "updated_at": now,
        }
        return self.db.upsert_hourly_report(payload)

    def generate_last_completed_interval_report(self, now: Optional[datetime] = None):
        report_date, window_start, window_end = self.build_last_completed_interval_window(now=now)
        return self.generate_hourly_report_for_window(report_date, window_start, window_end)

    def backfill_recent_hourly_reports(self, now: Optional[datetime] = None) -> int:
        local_now = (now or datetime.now(tz=self.local_tz)).astimezone(self.local_tz)
        utc_now = local_now.astimezone(timezone.utc)
        created = 0
        for report_date in [local_now.date() - timedelta(days=1), local_now.date()]:
            for _, window_start, window_end in self.iter_interval_windows_for_date(report_date):
                if window_end > utc_now:
                    continue
                if self.db.get_hourly_report_by_window(window_start, window_end, self.timezone_name) is not None:
                    continue
                self.generate_hourly_report_for_window(report_date, window_start, window_end)
                created += 1
        return created

    def _merge_hourly_reports(self, report_date: date, hourly_reports: list[Any]) -> dict[str, Any]:
        if not hourly_reports:
            _, window_start, window_end = self.build_previous_day_window(
                now=datetime.combine(report_date + timedelta(days=1), dt_time.min, tzinfo=self.local_tz)
            )
            return build_empty_summary(
                report_date=report_date,
                timezone_name=self.timezone_name,
                window_start=window_start,
                window_end=window_end,
            )

        window_start = min(report.window_start for report in hourly_reports)
        window_end = max(report.window_end for report in hourly_reports)
        messages = self.db.get_messages_for_window(window_start, window_end)
        filter_details: dict[str, int] = {}
        channel_ids = set()
        for report in hourly_reports:
            payload = report.content_json or {}
            channel_ids.update(payload.get("channel_ids", []))
            for key, value in (payload.get("filter_details") or {}).items():
                filter_details[key] = filter_details.get(key, 0) + int(value)

        summary = {
            "report_date": report_date.isoformat(),
            "timezone": self.timezone_name,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "source_message_count": sum(int(report.source_message_count or 0) for report in hourly_reports),
            "target_message_count": sum(int(report.target_message_count or 0) for report in hourly_reports),
            "candidate_message_count": sum(int(report.candidate_message_count or 0) for report in hourly_reports),
            "active_user_count": len({str(getattr(msg, "author_id", "")) for msg in messages}),
            "channel_ids": sorted(channel_ids),
            "shard_count": sum(int(report.shard_count or 0) for report in hourly_reports),
            "interval_count": len(hourly_reports),
            "filter_details": filter_details,
        }
        child_summaries = [report.content_json or {} for report in hourly_reports]
        summary["urgent_issues"] = _merge_section_items(child_summaries, "urgent_issues")
        summary["product_opportunities"] = _merge_section_items(child_summaries, "product_opportunities")
        summary["general_feedback"] = _merge_section_items(child_summaries, "general_feedback")
        summary["sentiment"] = _derive_sentiment(summary)
        return summary

    def generate_daily_report_from_hourly_reports(self, report_date: date):
        expected_windows = self.iter_interval_windows_for_date(report_date)
        for _, window_start, window_end in expected_windows:
            if self.db.get_hourly_report_by_window(window_start, window_end, self.timezone_name) is None:
                self.generate_hourly_report_for_window(report_date, window_start, window_end)

        hourly_reports = self.db.get_hourly_reports_for_date(report_date)
        summary = self._merge_hourly_reports(report_date, hourly_reports)
        markdown = (
            _empty_daily_markdown(report_date, _ensure_utc(expected_windows[0][1]), _ensure_utc(expected_windows[-1][2]))
            if summary["source_message_count"] == 0
            else render_daily_markdown(summary)
        )
        content_cn = (
            markdown
            if self.translator is None or summary["candidate_message_count"] == 0
            else self.translator.compose_daily_markdown(summary)
        )
        now = datetime.now(tz=timezone.utc)
        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "window_start": _ensure_utc(expected_windows[0][1]),
            "window_end": _ensure_utc(expected_windows[-1][2]),
            "content": markdown,
            "content_cn": content_cn,
            "source_message_count": summary["source_message_count"],
            "target_message_count": summary["target_message_count"],
            "generated_at": now,
            "updated_at": now,
        }
        return self.db.upsert_daily_report(payload)

    def generate_previous_day_report(self, now: Optional[datetime] = None):
        report_date, _, _ = self.build_previous_day_window(now=now)
        return self.generate_daily_report_from_hourly_reports(report_date)

    def generate_today_so_far_report(self, now: Optional[datetime] = None):
        report_date, window_start, window_end = self.build_today_so_far_window(now=now)
        summary = self._build_summary_from_messages(
            report_date=report_date,
            window_start=window_start,
            window_end=window_end,
        )
        summary["interval_count"] = max(1, int((window_end - window_start).total_seconds() // (self.interval_hours * 3600)))
        markdown = (
            _empty_daily_markdown(report_date, window_start, window_end)
            if summary["source_message_count"] == 0
            else render_daily_markdown(summary)
        )
        content_cn = (
            markdown
            if self.translator is None or summary["candidate_message_count"] == 0
            else self.translator.compose_daily_markdown(summary)
        )
        current_time = datetime.now(tz=timezone.utc)
        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "window_start": window_start,
            "window_end": window_end,
            "content": markdown,
            "content_cn": content_cn,
            "source_message_count": summary["source_message_count"],
            "target_message_count": summary["target_message_count"],
            "generated_at": current_time,
            "updated_at": current_time,
        }
        return self.db.upsert_daily_report(payload)
