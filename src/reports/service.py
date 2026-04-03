from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from ..pipeline import build_interval_pipeline_bundle_from_messages
from ..storage import Database


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
SECTION_LIMITS = {
    "urgent_issues": 16,
    "product_opportunities": 12,
    "general_feedback": 10,
}


@dataclass(frozen=True)
class ReportScope:
    scope_type: str
    scope_key: str
    region_key: str = "__all__"
    region_name: str = "全部"
    channel_id: int = 0
    channel_name: str = "全部频道"

    @property
    def display_name(self) -> str:
        if self.scope_type == "global":
            return "全部频道"
        if self.scope_type == "region":
            return f"{self.region_name} 国区"
        return f"{self.region_name} / {self.channel_name}"


def build_report_scopes(targets: Optional[dict[str, Any]]) -> list[ReportScope]:
    scopes: list[ReportScope] = []
    targets = targets or {}
    seen: set[str] = set()
    for region in targets.get("regions", []) or []:
        region_key = str(region.get("key") or "__all__").strip() or "__all__"
        region_name = str(region.get("name") or region_key or "Region").strip()
        channels = region.get("channels", []) or []
        has_channels = False
        for channel in channels:
            channel_id = int(channel.get("id") or 0)
            if channel_id <= 0:
                continue
            has_channels = True
            scope_key = f"{region_key}:{channel_id}"
            if scope_key in seen:
                continue
            seen.add(scope_key)
            scopes.append(
                ReportScope(
                    scope_type="channel",
                    scope_key=scope_key,
                    region_key=region_key,
                    region_name=region_name,
                    channel_id=channel_id,
                    channel_name=str(channel.get("name") or f"channel {channel_id}").strip(),
                )
            )
        if has_channels and region_key not in seen:
            seen.add(region_key)
            scopes.append(
                ReportScope(
                    scope_type="region",
                    scope_key=region_key,
                    region_key=region_key,
                    region_name=region_name,
                    channel_id=0,
                    channel_name=f"{region_name} 全部频道",
                )
            )
    return scopes


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


def _extract_text_from_response_body(body: dict[str, Any]) -> str:
    text_parts: list[str] = []

    def _append(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            text_parts.append(text)

    content = body.get("content")
    if isinstance(content, str):
        _append(content)
    elif isinstance(content, list):
        for chunk in content:
            if isinstance(chunk, str):
                _append(chunk)
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") == "text":
                _append(chunk.get("text"))
                continue
            if isinstance(chunk.get("text"), str):
                _append(chunk.get("text"))
                continue
            if isinstance(chunk.get("content"), str):
                _append(chunk.get("content"))

    if isinstance(body.get("output_text"), str):
        _append(body.get("output_text"))
    if isinstance(body.get("completion"), str):
        _append(body.get("completion"))
    if isinstance(body.get("text"), str):
        _append(body.get("text"))

    choices = body.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if isinstance(choice.get("text"), str):
                _append(choice.get("text"))
            message = choice.get("message")
            if isinstance(message, dict):
                message_content = message.get("content")
                if isinstance(message_content, str):
                    _append(message_content)
                elif isinstance(message_content, list):
                    for chunk in message_content:
                        if isinstance(chunk, str):
                            _append(chunk)
                        elif isinstance(chunk, dict):
                            _append(chunk.get("text") or chunk.get("content"))
            delta = choice.get("delta")
            if isinstance(delta, dict):
                _append(delta.get("text") or delta.get("content"))

    return "\n".join(part for part in text_parts if part).strip()


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
            f"- 语言分布: {json.dumps(summary.get('detected_language_breakdown', {}), ensure_ascii=False)}",
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
    scope: Optional[ReportScope] = None,
) -> dict[str, Any]:
    scope = scope or ReportScope(scope_type="global", scope_key="global")
    return {
        "report_date": report_date.isoformat(),
        "timezone": timezone_name,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "region_key": scope.region_key,
        "region_name": scope.region_name,
        "channel_id": scope.channel_id,
        "channel_name": scope.channel_name,
        "source_message_count": 0,
        "candidate_message_count": 0,
        "active_user_count": 0,
        "channel_ids": [],
        "shard_count": 0,
        "interval_count": 0,
        "detected_language_breakdown": {},
        "filter_details": {},
        "urgent_issues": [],
        "product_opportunities": [],
        "general_feedback": [],
        "sentiment": {"score": "neutral", "reason": "当天没有新的已收集消息。"},
    }


@dataclass
class LLMBudgetConfig:
    quota_per_5h: int = 4500
    utilization_ratio: float = 0.88
    window_hours: int = 5
    max_parallel_requests: int = 24
    max_scope_workers: int = 6
    target_items_per_shard: int = 28
    min_shard_chars: int = 6_000
    max_shard_chars: int = 32_000
    reserve_calls: int = 120

    @property
    def effective_call_limit(self) -> int:
        return max(1, int(self.quota_per_5h * self.utilization_ratio))

    @classmethod
    def from_config(cls, raw: Optional[dict[str, Any]]) -> "LLMBudgetConfig":
        raw = raw or {}
        return cls(
            quota_per_5h=int(raw.get("quota_per_5h", 4500)),
            utilization_ratio=float(raw.get("utilization_ratio", 0.88)),
            window_hours=int(raw.get("window_hours", 5)),
            max_parallel_requests=max(1, int(raw.get("max_parallel_requests", 24))),
            max_scope_workers=max(1, int(raw.get("max_scope_workers", 6))),
            target_items_per_shard=max(1, int(raw.get("target_items_per_shard", 28))),
            min_shard_chars=max(2_000, int(raw.get("min_shard_chars", 6_000))),
            max_shard_chars=max(4_000, int(raw.get("max_shard_chars", 32_000))),
            reserve_calls=max(0, int(raw.get("reserve_calls", 120))),
        )


class RollingCallBudget:
    def __init__(self, config: LLMBudgetConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()

    def _prune(self, now_ts: float) -> None:
        cutoff = now_ts - (self.config.window_hours * 3600)
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

    def remaining_calls(self) -> int:
        with self._lock:
            now_ts = time.time()
            self._prune(now_ts)
            return max(0, self.config.effective_call_limit - len(self._calls))

    def acquire_slot(self) -> None:
        while True:
            with self._lock:
                now_ts = time.time()
                self._prune(now_ts)
                if len(self._calls) < self.config.effective_call_limit:
                    self._calls.append(now_ts)
                    return
                wait_seconds = (self.config.window_hours * 3600) - (now_ts - self._calls[0]) + 0.05
            time.sleep(max(0.05, min(wait_seconds, 5.0)))


@dataclass
class DailyReportTranslator:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 45.0
    budget_config: LLMBudgetConfig = field(default_factory=LLMBudgetConfig)

    @classmethod
    def from_env(cls, budget_config: Optional[LLMBudgetConfig] = None) -> "DailyReportTranslator":
        base_url = os.getenv("LLM_BASE_URL", "").strip()
        model = os.getenv("LLM_MODEL", "").strip()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if not base_url or not model or not api_key:
            raise RuntimeError("Missing LLM_BASE_URL, LLM_MODEL, or LLM_API_KEY for report generation")
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            budget_config=budget_config or LLMBudgetConfig(),
        )

    def __post_init__(self) -> None:
        self._budget = RollingCallBudget(self.budget_config)
        self._request_slots = threading.BoundedSemaphore(self.budget_config.max_parallel_requests)

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

    def build_execution_plan(self, report: dict[str, Any]) -> dict[str, int]:
        remaining_calls = self._budget.remaining_calls()
        usable_calls = max(1, remaining_calls - self.budget_config.reserve_calls)
        target_shards = max(1, math.ceil(int(report.get("candidate_group_count", 0)) / self.budget_config.target_items_per_shard))
        planned_shards = max(1, min(usable_calls, target_shards))
        candidate_chars = int(report.get("candidate_payload_chars", 0))
        if candidate_chars <= 0:
            shard_char_budget = self.budget_config.max_shard_chars
        else:
            shard_char_budget = math.ceil(candidate_chars / planned_shards)
            shard_char_budget = max(self.budget_config.min_shard_chars, shard_char_budget)
            shard_char_budget = min(self.budget_config.max_shard_chars, shard_char_budget)
        parallel_requests = max(1, min(self.budget_config.max_parallel_requests, planned_shards, usable_calls))
        return {
            "remaining_calls": remaining_calls,
            "usable_calls": usable_calls,
            "planned_shards": planned_shards,
            "shard_char_budget": shard_char_budget,
            "parallel_requests": parallel_requests,
            "effective_call_limit": self.budget_config.effective_call_limit,
        }

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
                self._budget.acquire_slot()
                with self._request_slots:
                    with httpx.Client(timeout=timeout) as client:
                        response = client.post(self._messages_url(), headers=headers, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                body = response.json()
                text = _extract_text_from_response_body(body)
                if not text:
                    if attempt < 3:
                        time.sleep(2 ** (attempt - 1))
                        continue
                    preview = json.dumps(body, ensure_ascii=False)[:600]
                    raise RuntimeError(f"LLM response did not contain text content: {preview}")
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

    def empty_summary(self, reason: str = "分片总结失败，已跳过。") -> dict[str, Any]:
        return {
            "urgent_issues": [],
            "product_opportunities": [],
            "general_feedback": [],
            "sentiment": {"score": "neutral", "reason": reason},
        }

    def summarize_signal_shard(self, shard: dict[str, Any]) -> dict[str, Any]:
        system = (
            "你是产品情报分析助手。"
            "你会阅读一批 Discord 消息候选信号，并输出严格 JSON。"
            "尽量保留有价值的问题、机会、一般反馈和关键证据。"
            "忽略明显无关的寒暄，但不要过度丢弃潜在有用信息。"
            "不要输出 markdown，不要输出解释，不要输出 JSON 之外的任何文本。"
        )
        user_content = (
            "请返回一个 JSON 对象，并且只能包含下面这些顶层键：\n"
            "{\n"
            '  "urgent_issues": [...],\n'
            '  "product_opportunities": [...],\n'
            '  "general_feedback": [...],\n'
            '  "sentiment": {"score": "negative|neutral|positive", "reason": "简短中文原因"}\n'
            "}\n\n"
            "字段规则：\n"
            '- 每个条目都必须包含："key", "category", "priority", "title", "summary", '
            '"message_count", "unique_user_count", "channel_ids", "evidence", "action_hint"\n'
            '- "key" 必须是稳定的 english_snake_case\n'
            '- "priority" 只能是 high / medium / low\n'
            '- "title" 和 "summary" 必须使用简体中文\n'
            '- "evidence" 保留 1 到 4 条原始语言短摘录\n'
            '- 尽量合并语义接近的问题，保留更充分的证据和影响范围\n'
            '- urgent_issues 最多 10 项，product_opportunities 最多 8 项，general_feedback 最多 6 项\n'
            '- 如果产品信号很弱，返回空数组并给出 neutral sentiment\n\n'
            f"下面是待总结的 JSON 数据：\n{json.dumps(shard, ensure_ascii=False)}"
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
            "你是产品情报分析助手。"
            "阅读用户提供的 JSON 数据，只根据 JSON 本身写出中文报告。"
            "直接生成自然、清晰、有重点的中文报告，不要套固定模板。"
            "可以自行组织结构，但不要编造不存在的信息，不要输出 JSON。"
        )
        user_content = f"把下面的 JSON 总结成中文报告交给我：\n{json.dumps(summary, ensure_ascii=False)}"
        return self._request_text(system=system, user_content=user_content, max_tokens=6144).strip()

    def summarize_channel_text_shard(self, shard: dict[str, Any], scope: ReportScope, report_date: date) -> str:
        system = (
            "你是产品情报分析助手。"
            "你会阅读单个 Discord 频道在一个时间片内的候选消息 JSON，并直接输出中文子报告。"
            "不要输出 JSON，不要解释提示词，只输出简洁、专业、可合并的中文报告。"
            "优先保留问题、用户诉求、明显趋势和代表性证据。"
        )
        user_content = (
            f"频道：{scope.display_name}\n"
            f"报告日期：{report_date.isoformat()}\n"
            f"时间窗口：{shard.get('window_start')} 至 {shard.get('window_end')}\n"
            "请直接生成一份中文子报告，长度尽量控制在 300 到 600 字之间。\n"
            "如果这一批里高价值信号不多，也请把真正有用的信息说清楚，不要为了凑字数编造内容。\n"
            "下面是候选消息 JSON：\n"
            f"{json.dumps(shard, ensure_ascii=False)}"
        )
        return self._request_text(system=system, user_content=user_content, max_tokens=2048).strip()

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
        max_batch_chars: int = 40_000,
        depth: int = 0,
    ) -> str:
        cleaned_reports = [text.strip() for text in shard_reports if str(text or "").strip()]
        if not cleaned_reports:
            return (
                f"{scope.display_name} 在 {report_date.isoformat()} 00:00 至当前时间内没有足够的有效信号，"
                "暂时无法生成有意义的中文日报。"
            )

        def _chunk_texts(texts: list[str], budget: int) -> list[list[str]]:
            groups: list[list[str]] = []
            current: list[str] = []
            current_chars = 0
            for text in texts:
                size = len(text)
                if current and current_chars + size > budget:
                    groups.append(current)
                    current = []
                    current_chars = 0
                current.append(text)
                current_chars += size
            if current:
                groups.append(current)
            return groups

        total_chars = sum(len(text) for text in cleaned_reports)
        if len(cleaned_reports) > 6 or total_chars > max_batch_chars:
            merged_chunks = []
            for index, batch in enumerate(_chunk_texts(cleaned_reports, max_batch_chars), start=1):
                merged_chunks.append(
                    self.merge_channel_text_reports(
                        scope=scope,
                        report_date=report_date,
                        window_start=window_start,
                        window_end=window_end,
                        shard_reports=batch,
                        source_message_count=source_message_count,
                        candidate_message_count=candidate_message_count,
                        active_user_count=active_user_count,
                        max_batch_chars=max_batch_chars,
                        depth=depth + 1,
                    )
                )
            if len(merged_chunks) == 1:
                return merged_chunks[0]
            return self.merge_channel_text_reports(
                scope=scope,
                report_date=report_date,
                window_start=window_start,
                window_end=window_end,
                shard_reports=merged_chunks,
                source_message_count=source_message_count,
                candidate_message_count=candidate_message_count,
                active_user_count=active_user_count,
                max_batch_chars=max_batch_chars,
                depth=depth + 1,
            )

        system = (
            "你是产品情报分析助手。"
            "你会把同一频道同一天的多段中文子报告合并成一份最终中文日报。"
            "要求去重、合并同类问题、保留重要证据和趋势，不要重复表述。"
            "不要输出 JSON，不要说明你的处理过程，直接输出最终中文日报。"
        )
        user_content = (
            f"频道：{scope.display_name}\n"
            f"报告日期：{report_date.isoformat()}\n"
            f"时间窗口：{window_start.isoformat()} 至 {window_end.isoformat()}\n"
            f"总消息数：{source_message_count}\n"
            f"进入分析的候选消息数：{candidate_message_count}\n"
            f"活跃用户数：{active_user_count}\n"
            f"子报告数量：{len(cleaned_reports)}\n"
            "请把下面这些中文子报告合并成一份自然、清晰、面向产品/运营可读的最终中文日报：\n\n"
            + "\n\n".join(f"子报告 {index}：\n{text}" for index, text in enumerate(cleaned_reports, start=1))
        )
        return self._request_text(system=system, user_content=user_content, max_tokens=6144).strip()


class DailyReportService:
    def __init__(
        self,
        db: Database,
        translator: Optional[DailyReportTranslator] = None,
        timezone_name: str = "Asia/Shanghai",
        interval_hours: int = 2,
        scopes: Optional[list[ReportScope]] = None,
    ) -> None:
        self.db = db
        self.translator = translator
        self.timezone_name = timezone_name
        self.local_tz = ZoneInfo(timezone_name)
        self.interval_hours = interval_hours
        self.scopes = scopes or []

    def _max_scope_workers(self) -> int:
        if self.translator is None:
            return 1
        budget_config = getattr(self.translator, "budget_config", None)
        return max(1, int(getattr(budget_config, "max_scope_workers", 1)))

    def get_daily_report(self, report_date: date, scope_key: str = "global"):
        return self.db.get_daily_report_by_date(report_date, scope_key=scope_key)

    def iter_scopes(self, include_global: bool = True) -> list[ReportScope]:
        if include_global:
            return list(self.scopes)
        return [scope for scope in self.scopes if scope.scope_type != "global"]

    def iter_hourly_scopes(self) -> list[ReportScope]:
        """Return scopes that need hourly reports (channel only, NOT region)."""
        return [scope for scope in self.scopes if scope.scope_type == "channel"]

    def iter_daily_scopes(self) -> list[ReportScope]:
        """Return scopes that need daily reports (region only)."""
        return [scope for scope in self.scopes if scope.scope_type == "region"]

    def get_channel_scopes_for_region(self, region_key: str) -> list[ReportScope]:
        """Return all channel scopes belonging to a given region."""
        return [
            scope for scope in self.scopes
            if scope.scope_type == "channel" and scope.region_key == region_key
        ]

    def get_scope_for_channel(self, channel_id: int) -> ReportScope:
        for scope in self.iter_scopes(include_global=False):
            if int(scope.channel_id or 0) == int(channel_id):
                return scope
        return ReportScope(
            scope_type="channel",
            scope_key=f"manual:{int(channel_id)}",
            region_key="manual",
            region_name="手动频道",
            channel_id=int(channel_id),
            channel_name=f"channel {int(channel_id)}",
        )

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

    def _split_shard(self, shard: dict[str, Any], suffix: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "shard_id": f"{shard.get('shard_id', 'shard')}-{suffix}",
            "window_start": shard.get("window_start"),
            "window_end": shard.get("window_end"),
            "stats": {
                "candidate_count": len(items),
                "message_count": sum(int(entry.get("message_count", 0)) for entry in items),
                "channel_count": len({entry.get("channel_id") for entry in items if entry.get("channel_id")}),
            },
            "items": items,
        }

    def _summarize_signal_shard_resilient(
        self,
        shard: dict[str, Any],
        *,
        scope: ReportScope,
        depth: int = 0,
    ) -> dict[str, Any]:
        if self.translator is None:
            raise RuntimeError("LLM translator is required for shard summarization")
        try:
            return self.translator.summarize_signal_shard(shard)
        except Exception as exc:
            items = list(shard.get("items") or [])
            if len(items) > 1 and depth < 2:
                print(
                    f"[llm] retry-split {scope.display_name} shard={shard.get('shard_id')} "
                    f"items={len(items)} reason={type(exc).__name__}"
                )
                midpoint = max(1, len(items) // 2)
                left = self._split_shard(shard, "a", items[:midpoint])
                right = self._split_shard(shard, "b", items[midpoint:])
                partials = [
                    self._summarize_signal_shard_resilient(left, scope=scope, depth=depth + 1),
                    self._summarize_signal_shard_resilient(right, scope=scope, depth=depth + 1),
                ]
                merged = {
                    "urgent_issues": _merge_section_items(partials, "urgent_issues"),
                    "product_opportunities": _merge_section_items(partials, "product_opportunities"),
                    "general_feedback": _merge_section_items(partials, "general_feedback"),
                }
                merged["sentiment"] = _derive_sentiment(merged)
                return merged

            print(
                f"[llm] skip {scope.display_name} shard={shard.get('shard_id')} "
                f"reason={type(exc).__name__}: {exc}"
            )
            return self.translator.empty_summary()

    def _summarize_channel_text_shard_resilient(
        self,
        shard: dict[str, Any],
        *,
        scope: ReportScope,
        report_date: date,
        depth: int = 0,
    ) -> list[str]:
        if self.translator is None:
            raise RuntimeError("LLM translator is required for channel text summarization")
        try:
            text = self.translator.summarize_channel_text_shard(shard, scope=scope, report_date=report_date)
            return [text] if text.strip() else []
        except Exception as exc:
            items = list(shard.get("items") or [])
            if len(items) > 1 and depth < 2:
                print(
                    f"[channel-llm] retry-split {scope.display_name} shard={shard.get('shard_id')} "
                    f"items={len(items)} reason={type(exc).__name__}"
                )
                midpoint = max(1, len(items) // 2)
                left = self._split_shard(shard, "a", items[:midpoint])
                right = self._split_shard(shard, "b", items[midpoint:])
                return (
                    self._summarize_channel_text_shard_resilient(left, scope=scope, report_date=report_date, depth=depth + 1)
                    + self._summarize_channel_text_shard_resilient(right, scope=scope, report_date=report_date, depth=depth + 1)
                )
            print(
                f"[channel-llm] skip {scope.display_name} shard={shard.get('shard_id')} "
                f"reason={type(exc).__name__}: {exc}"
            )
            return []

    def _build_summary_from_messages(
        self,
        *,
        report_date: date,
        window_start: datetime,
        window_end: datetime,
        scope: Optional[ReportScope] = None,
    ) -> dict[str, Any]:
        scope = scope or ReportScope(scope_type="global", scope_key="global")
        utc_start = _ensure_utc(window_start)
        utc_end = _ensure_utc(window_end)
        if scope.scope_type == "region":
            messages = self.db.get_messages_for_window(
                utc_start,
                utc_end,
                region_key=scope.region_key,
            )
        else:
            messages = self.db.get_messages_for_window(
                utc_start,
                utc_end,
                channel_id=scope.channel_id or None,
                scope_key=scope.scope_key if scope.scope_type != "global" else None,
            )
        if not messages:
            return build_empty_summary(
                report_date=report_date,
                timezone_name=self.timezone_name,
                window_start=utc_start,
                window_end=utc_end,
                scope=scope,
            )

        bundle = build_interval_pipeline_bundle_from_messages(
            messages=messages,
            report_date=report_date,
            timezone_name=self.timezone_name,
            window_start=utc_start,
            window_end=utc_end,
        )
        if self.translator is not None and bundle["report"]["candidate_group_count"] > 0:
            initial_plan = self.translator.build_execution_plan(bundle["report"])
            planned_budget = int(initial_plan["shard_char_budget"])
            if planned_budget != bundle["report"].get("shard_char_budget", 12_000):
                bundle = build_interval_pipeline_bundle_from_messages(
                    messages=messages,
                    report_date=report_date,
                    timezone_name=self.timezone_name,
                    window_start=utc_start,
                    window_end=utc_end,
                    shard_char_budget=planned_budget,
                )
        report = bundle["report"]
        base_summary = {
            "report_date": report["report_date"],
            "timezone": report["timezone"],
            "window_start": report["window_start"],
            "window_end": report["window_end"],
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "region_name": scope.region_name,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "source_message_count": report["source_message_count"],
            "candidate_message_count": report["candidate_message_count"],
            "active_user_count": report["active_user_count"],
            "channel_ids": sorted({str(getattr(msg, "channel_id", "")) for msg in messages if getattr(msg, "channel_id", None)}),
            "shard_count": report["shard_count"],
            "interval_count": 1,
            "detected_language_breakdown": report.get("detected_language_breakdown", {}),
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

        execution_plan = self.translator.build_execution_plan(report)
        shard_summaries = []
        total_shards = len(bundle["shards"])
        if total_shards > 1:
            print(
                f"[llm] {scope.display_name} shards={total_shards} "
                f"candidates={report['candidate_group_count']} parallel={execution_plan['parallel_requests']} "
                f"budget={execution_plan['remaining_calls']}/{execution_plan['effective_call_limit']}"
            )
        parallel_requests = int(execution_plan["parallel_requests"])
        if parallel_requests <= 1 or total_shards <= 1:
            for index, shard in enumerate(bundle["shards"], start=1):
                if total_shards > 1:
                    print(f"[llm] {scope.display_name} {index}/{total_shards} chunks")
                shard_summaries.append(self._summarize_signal_shard_resilient(shard, scope=scope))
        else:
            shard_summaries = [None] * total_shards
            with ThreadPoolExecutor(max_workers=parallel_requests) as executor:
                future_map = {
                    executor.submit(self._summarize_signal_shard_resilient, shard, scope=scope): index
                    for index, shard in enumerate(bundle["shards"], start=1)
                }
                for future in as_completed(future_map):
                    index = future_map[future]
                    print(f"[llm] {scope.display_name} done {index}/{total_shards}")
                    shard_summaries[index - 1] = future.result()
        summary = dict(base_summary)
        summary["urgent_issues"] = _merge_section_items(shard_summaries, "urgent_issues")
        summary["product_opportunities"] = _merge_section_items(shard_summaries, "product_opportunities")
        summary["general_feedback"] = _merge_section_items(shard_summaries, "general_feedback")
        summary["sentiment"] = _derive_sentiment(summary)
        return summary

    def generate_hourly_report_for_window(
        self,
        report_date: date,
        window_start: datetime,
        window_end: datetime,
        scope: Optional[ReportScope] = None,
    ):
        scope = scope or ReportScope(scope_type="global", scope_key="global")
        summary = self._build_summary_from_messages(
            report_date=report_date,
            window_start=window_start,
            window_end=window_end,
            scope=scope,
        )
        now = datetime.now(tz=timezone.utc)
        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "window_start": _ensure_utc(window_start),
            "window_end": _ensure_utc(window_end),
            "content_json": summary,
            "source_message_count": summary["source_message_count"],
            "candidate_message_count": summary["candidate_message_count"],
            "shard_count": summary.get("shard_count", 0),
            "generated_at": now,
            "updated_at": now,
        }
        return self.db.upsert_hourly_report(payload)

    def generate_last_completed_interval_report(self, now: Optional[datetime] = None):
        report_date, window_start, window_end = self.build_last_completed_interval_window(now=now)
        return self.generate_hourly_report_for_window(report_date, window_start, window_end)

    def generate_last_completed_interval_reports(self, now: Optional[datetime] = None) -> list[Any]:
        report_date, window_start, window_end = self.build_last_completed_interval_window(now=now)
        return self.generate_hourly_reports_for_window(report_date, window_start, window_end)

    def generate_hourly_reports_for_window(
        self,
        report_date: date,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Any]:
        hourly_scopes = self.iter_hourly_scopes()
        results: list[Any] = []
        scope_workers = min(len(hourly_scopes), self._max_scope_workers())
        if scope_workers <= 1 or len(hourly_scopes) <= 1:
            for scope in hourly_scopes:
                results.append(self.generate_hourly_report_for_window(report_date, window_start, window_end, scope=scope))
            return results

        with ThreadPoolExecutor(max_workers=scope_workers) as executor:
            future_map = {
                executor.submit(self.generate_hourly_report_for_window, report_date, window_start, window_end, scope): scope
                for scope in hourly_scopes
            }
            for future in as_completed(future_map):
                scope = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    print(f"[hourly] skip {scope.display_name} reason={type(exc).__name__}: {exc}")
        return sorted(results, key=lambda item: (item.scope_type != "global", item.region_key, item.channel_name))

    def _merge_hourly_reports(self, report_date: date, hourly_reports: list[Any], scope: Optional[ReportScope] = None) -> dict[str, Any]:
        scope = scope or ReportScope(scope_type="global", scope_key="global")
        if not hourly_reports:
            _, window_start, window_end = self.build_previous_day_window(
                now=datetime.combine(report_date + timedelta(days=1), dt_time.min, tzinfo=self.local_tz)
            )
            return build_empty_summary(
                report_date=report_date,
                timezone_name=self.timezone_name,
                window_start=window_start,
                window_end=window_end,
                scope=scope,
            )

        window_start = min(report.window_start for report in hourly_reports)
        window_end = max(report.window_end for report in hourly_reports)
        if scope.scope_type == "region":
            messages = self.db.get_messages_for_window(
                window_start,
                window_end,
                region_key=scope.region_key,
            )
        else:
            messages = self.db.get_messages_for_window(
                window_start,
                window_end,
                channel_id=scope.channel_id or None,
                scope_key=scope.scope_key if scope.scope_type != "global" else None,
            )
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
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "region_name": scope.region_name,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "source_message_count": sum(int(report.source_message_count or 0) for report in hourly_reports),
            "candidate_message_count": sum(int(report.candidate_message_count or 0) for report in hourly_reports),
            "active_user_count": len({str(getattr(msg, "author_id", "")) for msg in messages}),
            "channel_ids": sorted(channel_ids),
            "shard_count": sum(int(report.shard_count or 0) for report in hourly_reports),
            "interval_count": len(hourly_reports),
            "detected_language_breakdown": {},
            "filter_details": filter_details,
        }
        detected_language_breakdown: dict[str, int] = {}
        for report in hourly_reports:
            payload = report.content_json or {}
            for language, count in (payload.get("detected_language_breakdown") or {}).items():
                detected_language_breakdown[str(language)] = detected_language_breakdown.get(str(language), 0) + int(count)
        summary["detected_language_breakdown"] = detected_language_breakdown
        child_summaries = [report.content_json or {} for report in hourly_reports]
        summary["urgent_issues"] = _merge_section_items(child_summaries, "urgent_issues")
        summary["product_opportunities"] = _merge_section_items(child_summaries, "product_opportunities")
        summary["general_feedback"] = _merge_section_items(child_summaries, "general_feedback")
        summary["sentiment"] = _derive_sentiment(summary)
        return summary

    def generate_daily_report_from_hourly_reports(self, report_date: date, scope: Optional[ReportScope] = None):
        scope = scope or ReportScope(scope_type="global", scope_key="global")
        if scope.scope_type == "region":
            hourly_reports = self.db.get_hourly_reports_for_date_by_region(report_date, region_key=scope.region_key)
        else:
            hourly_reports = self.db.get_hourly_reports_for_date(report_date, scope_key=scope.scope_key)
        expected_windows = self.iter_interval_windows_for_date(report_date)
        summary = self._merge_hourly_reports(report_date, hourly_reports, scope=scope)
        markdown = (
            _empty_daily_markdown(report_date, _ensure_utc(expected_windows[0][1]), _ensure_utc(expected_windows[-1][2]))
            if summary["source_message_count"] == 0
            else render_daily_markdown(summary)
        )
        llm_daily_payload = {
            "kind": "daily_report_from_hourly_reports",
            "report_date": report_date.isoformat(),
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "region_name": scope.region_name,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "timezone": self.timezone_name,
            "window_start": _ensure_utc(expected_windows[0][1]).isoformat(),
            "window_end": _ensure_utc(expected_windows[-1][2]).isoformat(),
            "hourly_reports": [report.content_json or {} for report in hourly_reports],
        }
        content_cn = (
            markdown
            if self.translator is None or summary["candidate_message_count"] == 0
            else self.translator.compose_daily_markdown(llm_daily_payload)
        )
        now = datetime.now(tz=timezone.utc)
        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "window_start": _ensure_utc(expected_windows[0][1]),
            "window_end": _ensure_utc(expected_windows[-1][2]),
            "content": markdown,
            "content_cn": content_cn,
            "source_message_count": summary["source_message_count"],
            "candidate_message_count": summary["candidate_message_count"],
            "generated_at": now,
            "updated_at": now,
        }
        return self.db.upsert_daily_report(payload)

    def generate_previous_day_report(self, now: Optional[datetime] = None):
        report_date, _, _ = self.build_previous_day_window(now=now)
        return self.generate_daily_report_from_hourly_reports(report_date)

    def generate_daily_reports_for_date(self, report_date: date) -> list[Any]:
        daily_scopes = self.iter_daily_scopes()
        results: list[Any] = []
        scope_workers = min(len(daily_scopes), self._max_scope_workers())
        if scope_workers <= 1 or len(daily_scopes) <= 1:
            for scope in daily_scopes:
                results.append(self.generate_daily_report_from_hourly_reports(report_date, scope=scope))
            return results
        with ThreadPoolExecutor(max_workers=scope_workers) as executor:
            future_map = {
                executor.submit(self.generate_daily_report_from_hourly_reports, report_date, scope): scope
                for scope in daily_scopes
            }
            for future in as_completed(future_map):
                scope = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    print(f"[daily] skip {scope.display_name} reason={type(exc).__name__}: {exc}")
        return sorted(results, key=lambda item: (item.scope_type != "global", item.region_key, item.channel_name))

    def generate_previous_day_reports(self, now: Optional[datetime] = None) -> list[Any]:
        report_date, _, _ = self.build_previous_day_window(now=now)
        return self.generate_daily_reports_for_date(report_date)

    def generate_today_so_far_report(self, now: Optional[datetime] = None, scope: Optional[ReportScope] = None):
        scope = scope or ReportScope(scope_type="global", scope_key="global")
        report_date, window_start, window_end = self.build_today_so_far_window(now=now)
        summary = self._build_summary_from_messages(
            report_date=report_date,
            window_start=window_start,
            window_end=window_end,
            scope=scope,
        )
        summary["interval_count"] = max(1, int((window_end - window_start).total_seconds() // (self.interval_hours * 3600)))
        markdown = (
            _empty_daily_markdown(report_date, window_start, window_end)
            if summary["source_message_count"] == 0
            else render_daily_markdown(summary)
        )
        llm_preview_payload = {
            "kind": "today_so_far_preview",
            "report_date": report_date.isoformat(),
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "region_name": scope.region_name,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "timezone": self.timezone_name,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "hourly_reports": [summary],
        }
        content_cn = (
            markdown
            if self.translator is None or summary["candidate_message_count"] == 0
            else self.translator.compose_daily_markdown(llm_preview_payload)
        )
        current_time = datetime.now(tz=timezone.utc)
        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "window_start": window_start,
            "window_end": window_end,
            "content": markdown,
            "content_cn": content_cn,
            "source_message_count": summary["source_message_count"],
            "candidate_message_count": summary["candidate_message_count"],
            "generated_at": current_time,
            "updated_at": current_time,
        }
        return self.db.upsert_daily_report(payload)

    def generate_channel_today_text_report(self, channel_id: int, now: Optional[datetime] = None):
        if self.translator is None:
            raise RuntimeError("LLM translator is required for channel-specific report generation")

        scope = self.get_scope_for_channel(channel_id)
        report_date, window_start, window_end = self.build_today_so_far_window(now=now)
        utc_start = _ensure_utc(window_start)
        utc_end = _ensure_utc(window_end)
        messages = self.db.get_messages_for_window(utc_start, utc_end, channel_id=int(channel_id))

        if not messages:
            content = (
                f"{scope.display_name} 在 {report_date.isoformat()} 00:00 至当前时间内没有新的已收集消息，"
                "因此没有可生成的频道日报。"
            )
            current_time = datetime.now(tz=timezone.utc)
            return self.db.upsert_daily_report(
                {
                    "report_date": report_date,
                    "timezone": self.timezone_name,
                    "scope_type": scope.scope_type,
                    "scope_key": scope.scope_key,
                    "region_key": scope.region_key,
                    "channel_id": scope.channel_id,
                    "channel_name": scope.channel_name,
                    "window_start": utc_start,
                    "window_end": utc_end,
                    "content": content,
                    "content_cn": content,
                    "source_message_count": 0,
                    "candidate_message_count": 0,
                    "generated_at": current_time,
                    "updated_at": current_time,
                }
            )

        bundle = build_interval_pipeline_bundle_from_messages(
            messages=messages,
            report_date=report_date,
            timezone_name=self.timezone_name,
            window_start=utc_start,
            window_end=utc_end,
        )
        plan = self.translator.build_execution_plan(bundle["report"])
        planned_budget = int(plan["shard_char_budget"])
        if planned_budget != bundle["report"].get("shard_char_budget", 12_000):
            bundle = build_interval_pipeline_bundle_from_messages(
                messages=messages,
                report_date=report_date,
                timezone_name=self.timezone_name,
                window_start=utc_start,
                window_end=utc_end,
                shard_char_budget=planned_budget,
            )
            plan = self.translator.build_execution_plan(bundle["report"])

        report = bundle["report"]
        shard_reports: list[str] = []
        total_shards = len(bundle["shards"])
        if total_shards > 1:
            print(
                f"[channel-llm] {scope.display_name} shards={total_shards} "
                f"candidates={report['candidate_group_count']} parallel={plan['parallel_requests']} "
                f"budget={plan['remaining_calls']}/{plan['effective_call_limit']}"
            )

        parallel_requests = max(1, int(plan["parallel_requests"]))
        if parallel_requests <= 1 or total_shards <= 1:
            for index, shard in enumerate(bundle["shards"], start=1):
                if total_shards > 1:
                    print(f"[channel-llm] {scope.display_name} {index}/{total_shards} chunks")
                shard_reports.extend(
                    self._summarize_channel_text_shard_resilient(shard, scope=scope, report_date=report_date)
                )
        else:
            with ThreadPoolExecutor(max_workers=parallel_requests) as executor:
                future_map = {
                    executor.submit(
                        self._summarize_channel_text_shard_resilient,
                        shard,
                        scope=scope,
                        report_date=report_date,
                    ): index
                    for index, shard in enumerate(bundle["shards"], start=1)
                }
                for future in as_completed(future_map):
                    index = future_map[future]
                    print(f"[channel-llm] {scope.display_name} done {index}/{total_shards}")
                    shard_reports.extend(future.result())

        final_report = (
            self.translator.merge_channel_text_reports(
                scope=scope,
                report_date=report_date,
                window_start=utc_start,
                window_end=utc_end,
                shard_reports=shard_reports,
                source_message_count=int(report["source_message_count"]),
                candidate_message_count=int(report["candidate_message_count"]),
                active_user_count=int(report["active_user_count"]),
            )
            if shard_reports
            else (
                f"{scope.display_name} 在 {report_date.isoformat()} 00:00 至当前时间内收到了 "
                f"{int(report['source_message_count'])} 条消息，但暂时没有整理出足够稳定的中文子报告。"
            )
        )

        current_time = datetime.now(tz=timezone.utc)
        payload = {
            "report_date": report_date,
            "timezone": self.timezone_name,
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "region_key": scope.region_key,
            "channel_id": scope.channel_id,
            "channel_name": scope.channel_name,
            "window_start": utc_start,
            "window_end": utc_end,
            "content": final_report,
            "content_cn": final_report,
            "source_message_count": int(report["source_message_count"]),
            "candidate_message_count": int(report["candidate_message_count"]),
            "generated_at": current_time,
            "updated_at": current_time,
        }
        return self.db.upsert_daily_report(payload)
