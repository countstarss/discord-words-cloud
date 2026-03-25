#!/usr/bin/env python3
"""
Signal pipeline for interval and daily report generation.

This module keeps the rule layer intentionally cheap:
- normalize and lightly filter obvious noise
- keep multilingual product-relevant chatter
- aggregate repeated messages into compact candidates
- shard candidates into LLM-sized batches
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import pandas as pd


MIN_MSG_LENGTH = 3
LOW_QUALITY_THRESHOLD = 0.2
MAX_CANDIDATE_CONTENT_LENGTH = 280
MAX_GROUPING_TEXT_LENGTH = 420
DEFAULT_SHARD_CHAR_BUDGET = 28_000
DEFAULT_SHARD_MAX_ITEMS = 120
LONGFORM_STORY_LENGTH = 900

EMOJI_TAG_PATTERN = re.compile(r"<a?:\w+:\d+>")
URL_PATTERN = re.compile(r"https?://\S+")
HASHTAG_PATTERN = re.compile(r"(?:^|\s)#([\wก-๙-]{1,32})", re.UNICODE)
WHITESPACE_PATTERN = re.compile(r"\s+")
PUNCT_OR_SYMBOL_ONLY_PATTERN = re.compile(r"^[\W_]+$", re.UNICODE)
FILLER_PATTERNS = [
    re.compile(r"^[5๕]+$"),
    re.compile(r"^55+5*$"),
    re.compile(r"^[ha]+$", re.IGNORECASE),
    re.compile(r"^ครับ+$"),
    re.compile(r"^จ้า+$"),
    re.compile(r"^อ่า+$"),
    re.compile(r"^โอ้+$"),
    re.compile(r"^(<[^>]+>\s*)+$"),
]

QUESTION_HINTS = ("?", "？", "มั้ย", "ไหม", "หรือ", "ทำยังไง", "กดยังไง", "ได้ไหม", "where", "how")
PRODUCT_CONTEXT_HINTS = {
    "rubii",
    "app",
    "ai",
    "feature",
    "ui",
    "login",
    "update",
    "price",
    "premium",
    "coin",
    "gem",
    "bug",
    "error",
    "โหลด",
    "แอป",
    "แอพ",
    "ระบบ",
    "โค้ด",
    "ข้อความ",
    "ตอบ",
    "อัปเดต",
    "อัปเดท",
    "สมัคร",
    "เติม",
    "ราคา",
    "แพง",
    "ลิงก์",
    "ลิงค์",
    "หน้าจอ",
    "ฟีเจอร์",
    "ค้าง",
    "โหลด",
    "เข้าไม่ได้",
}
LONGFORM_STORY_HINTS = {
    "[เนื้อเรื่อง",
    "[เนื้อเรื่องย่อ",
    "เนื้อเรื่อง",
    "เรื่องย่อ",
    "โรล",
    "พล็อต",
    "โลกที่",
    "นักศึกษา",
    "โรงเรียน",
    "โรงพยาบาล",
    "มหาลัย",
    "มหาวิทยาลัย",
    "เพื่อนสนิท",
    "นิสัย",
    "ตัวละคร",
    "คาแรค",
}
SIGNAL_HINTS = {
    "bug_report": [
        "บัค",
        "bug",
        "error",
        "ล่ม",
        "crash",
        "ค้าง",
        "เปิดไม่ได้",
        "พัง",
        "ไม่ทำงาน",
        "เสีย",
    ],
    "performance": [
        "ช้า",
        "โหลดนาน",
        "โหลดไม่ขึ้น",
        "รอนาน",
        "slow",
        "lag",
        "หน่วง",
        "กระตุก",
    ],
    "update_issue": [
        "อัปเดท",
        "อัปเดต",
        "update",
        "เวอร์ชั่น",
        "เวอร์ชัน",
        "version",
        "ติดตั้ง",
        "install",
        "ดาวน์โหลด",
    ],
    "ai_quality": [
        "ตอบแปลก",
        "ตอบผิด",
        "ตอบซ้ำ",
        "ไม่เข้าใจ",
        "ตอบไม่ตรง",
        "พิมพ์แปลก",
        "หลุดคาแรค",
    ],
    "payment": [
        "จ่าย",
        "ราคา",
        "แพง",
        "ฟรี",
        "สมัคร",
        "premium",
        "เติม",
        "coin",
        "gem",
    ],
    "feature_request": [
        "อยากให้",
        "น่าจะมี",
        "ควรจะ",
        "เพิ่ม",
        "ฟีเจอร์",
        "feature",
        "ทำได้ไหม",
        "request",
        "suggest",
    ],
    "ux_confusion": [
        "กดตรงไหน",
        "ทำยังไง",
        "หาไม่เจอ",
        "สับสน",
        "อยู่ตรงไหน",
        "วิธี",
        "สอนหน่อย",
        "help",
    ],
}


def _empty_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "message_id",
            "guild_id",
            "channel_id",
            "author_id",
            "content",
            "created_at",
            "detected_language",
            "detected_language_confidence",
            "quality_score",
        ]
    )


def normalize_input_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_dataframe()

    normalized = df.copy()
    defaults = {
        "message_id": None,
        "guild_id": None,
        "channel_id": None,
        "author_id": "unknown",
        "content": "",
        "created_at": pd.Timestamp.now(tz="UTC"),
        "detected_language": "unknown",
        "detected_language_confidence": 0.0,
        "quality_score": 1.0,
    }
    for column, default in defaults.items():
        if column not in normalized.columns:
            normalized[column] = default

    if "detected_language" not in normalized.columns:
        if "language" in normalized.columns:
            normalized["detected_language"] = normalized["language"]
        else:
            normalized["detected_language"] = "unknown"
    if "detected_language_confidence" not in normalized.columns:
        if "lang_confidence" in normalized.columns:
            normalized["detected_language_confidence"] = normalized["lang_confidence"]
        else:
            normalized["detected_language_confidence"] = 0.0

    normalized["created_at"] = pd.to_datetime(normalized["created_at"], utc=True, errors="coerce")
    normalized["created_at"] = normalized["created_at"].fillna(pd.Timestamp.now(tz="UTC"))
    normalized["author_id"] = normalized["author_id"].fillna("unknown").astype(str)
    normalized["channel_id"] = normalized["channel_id"].fillna("unknown").astype(str)
    normalized["guild_id"] = normalized["guild_id"].fillna("unknown").astype(str)
    normalized["content"] = normalized["content"].fillna("").astype(str)
    normalized["detected_language"] = normalized["detected_language"].fillna("unknown").astype(str)
    normalized["detected_language_confidence"] = (
        pd.to_numeric(normalized["detected_language_confidence"], errors="coerce").fillna(0.0)
    )
    normalized["quality_score"] = pd.to_numeric(normalized["quality_score"], errors="coerce").fillna(1.0)
    return normalized.sort_values("created_at", kind="stable").reset_index(drop=True)


def messages_to_dataframe(messages: Iterable[Any]) -> pd.DataFrame:
    rows = []
    for message in messages:
        rows.append(
            {
                "message_id": getattr(message, "message_id", None),
                "guild_id": getattr(message, "guild_id", None),
                "channel_id": getattr(message, "channel_id", None),
                "author_id": getattr(message, "author_id", "unknown"),
                "content": getattr(message, "content", ""),
                "created_at": getattr(message, "created_at", None),
                "detected_language": getattr(message, "detected_language", "unknown"),
                "detected_language_confidence": getattr(message, "detected_language_confidence", 0.0),
                "quality_score": getattr(message, "quality_score", 1.0),
            }
        )
    return normalize_input_dataframe(pd.DataFrame(rows))


def normalize_message_text(content: str) -> str:
    text = str(content or "").replace("\u200b", " ").replace("\ufeff", " ")
    text = URL_PATTERN.sub(" ", text)
    text = EMOJI_TAG_PATTERN.sub(" ", text)
    text = WHITESPACE_PATTERN.sub(" ", text).strip()
    return text


def extract_hashtags(content: str) -> list[str]:
    tags = []
    seen = set()
    for match in HASHTAG_PATTERN.finditer(content or ""):
        tag = match.group(1).lower()
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def has_product_context(content_lower: str) -> bool:
    return any(hint in content_lower for hint in PRODUCT_CONTEXT_HINTS)


def looks_like_longform_story(content_lower: str) -> bool:
    if len(content_lower) < LONGFORM_STORY_LENGTH:
        return False
    story_hits = sum(1 for hint in LONGFORM_STORY_HINTS if hint in content_lower)
    return story_hits >= 2 or content_lower.count("\n") >= 3


def infer_hint_categories(content_lower: str) -> list[str]:
    categories = []
    for category, keywords in SIGNAL_HINTS.items():
        if any(keyword in content_lower for keyword in keywords):
            categories.append(category)
    return categories


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _is_pure_noise(normalized: str) -> bool:
    if not normalized:
        return True
    if PUNCT_OR_SYMBOL_ONLY_PATTERN.fullmatch(normalized):
        return True
    compact = normalized.replace(" ", "")
    if compact.isdigit():
        return True
    return any(pattern.match(compact) for pattern in FILLER_PATTERNS)


def should_drop_message(
    *,
    normalized_text: str,
    quality_score: float,
    has_question: bool,
    product_context: bool,
    hashtags: list[str],
) -> tuple[bool, str]:
    if not normalized_text:
        return True, "empty"
    if quality_score < LOW_QUALITY_THRESHOLD:
        return True, "low_quality"
    if _is_pure_noise(normalized_text):
        return True, "filler"
    if len(normalized_text) < MIN_MSG_LENGTH and not has_question and not hashtags:
        return True, "too_short"

    lower = normalized_text.lower()
    if looks_like_longform_story(lower) and not product_context and not has_question and not hashtags:
        return True, "longform_story"
    if len(normalized_text) > 1_500 and not product_context and not has_question and not hashtags:
        return True, "too_long_non_product"
    return False, ""


def _candidate_signal_score(candidate: dict[str, Any]) -> float:
    score = 0.15
    if candidate["product_context"]:
        score += 0.6
    if candidate["question"]:
        score += 0.45
    score += min(0.9, len(candidate["hint_categories"]) * 0.28)
    score += min(0.8, max(0, candidate["message_count"] - 1) * 0.12)
    score += min(0.8, max(0, candidate["unique_user_count"] - 1) * 0.16)
    score += min(0.3, len(candidate["hashtags"]) * 0.08)
    score += min(0.3, float(candidate["quality_score"]) * 0.1)
    return round(score, 3)


def _compact_item_size(item: dict[str, Any]) -> int:
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))


def _should_keep_candidate_for_llm(candidate: dict[str, Any]) -> bool:
    if candidate["hint_categories"]:
        return True
    if candidate["product_context"] or candidate["question"] or candidate["hashtags"]:
        return True
    if candidate["message_count"] > 1 or candidate["unique_user_count"] > 1:
        return True
    return float(candidate["signal_score"]) >= 0.9


def build_interval_pipeline_bundle_from_dataframe(
    df: pd.DataFrame,
    report_date: date | str,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
    shard_char_budget: int = DEFAULT_SHARD_CHAR_BUDGET,
    shard_max_items: int = DEFAULT_SHARD_MAX_ITEMS,
) -> dict[str, Any]:
    normalized = normalize_input_dataframe(df)
    report_date_str = report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    if normalized.empty:
        return {
            "report": {
                "report_date": report_date_str,
                "timezone": timezone_name,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "source_message_count": 0,
                "active_user_count": 0,
                "candidate_message_count": 0,
                "candidate_group_count": 0,
                "shard_count": 0,
                "detected_language_breakdown": {},
                "filter_details": {},
            },
            "candidates": [],
            "shards": [],
        }

    filter_counts: Counter[str] = Counter()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_message_count = 0

    for row in normalized.itertuples(index=False):
        raw_content = str(getattr(row, "content", "") or "")
        normalized_text = normalize_message_text(raw_content)
        hashtags = extract_hashtags(raw_content)
        content_lower = normalized_text.lower()
        product_context = has_product_context(content_lower)
        has_question = "?" in raw_content or "？" in raw_content or any(hint in content_lower for hint in QUESTION_HINTS)
        drop, reason = should_drop_message(
            normalized_text=normalized_text,
            quality_score=float(getattr(row, "quality_score", 1.0) or 1.0),
            has_question=has_question,
            product_context=product_context,
            hashtags=hashtags,
        )
        if drop:
            filter_counts[reason] += 1
            continue

        candidate_message_count += 1
        grouping_key = (str(getattr(row, "channel_id", "unknown")), normalized_text[:MAX_GROUPING_TEXT_LENGTH])
        group = groups.get(grouping_key)
        hint_categories = infer_hint_categories(content_lower)
        created_at = getattr(row, "created_at")
        created_at_iso = created_at.isoformat()
        if group is None:
            group = {
                "channel_id": str(getattr(row, "channel_id", "unknown")),
                "guild_id": str(getattr(row, "guild_id", "unknown")),
                "language_counter": Counter([str(getattr(row, "detected_language", "unknown"))]),
                "message_count": 1,
                "authors": {str(getattr(row, "author_id", "unknown"))},
                "quality_total": float(getattr(row, "quality_score", 1.0) or 1.0),
                "hashtags": set(hashtags),
                "hint_counter": Counter(hint_categories),
                "question": has_question,
                "product_context": product_context,
                "first_seen_at": created_at_iso,
                "last_seen_at": created_at_iso,
                "representative_content": raw_content.strip() or normalized_text,
                "normalized_content": normalized_text,
            }
            groups[grouping_key] = group
        else:
            group["language_counter"][str(getattr(row, "detected_language", "unknown"))] += 1
            group["message_count"] += 1
            group["authors"].add(str(getattr(row, "author_id", "unknown")))
            group["quality_total"] += float(getattr(row, "quality_score", 1.0) or 1.0)
            group["hashtags"].update(hashtags)
            group["hint_counter"].update(hint_categories)
            group["question"] = group["question"] or has_question
            group["product_context"] = group["product_context"] or product_context
            group["last_seen_at"] = created_at_iso
            if len(raw_content.strip()) > len(group["representative_content"]):
                group["representative_content"] = raw_content.strip()

    candidates = []
    for index, group in enumerate(groups.values(), start=1):
        top_language = group["language_counter"].most_common(1)[0][0] if group["language_counter"] else "unknown"
        item = {
            "candidate_id": f"cand-{index:05d}",
            "channel_id": group["channel_id"],
            "guild_id": group["guild_id"],
            "created_at": group["first_seen_at"],
            "last_seen_at": group["last_seen_at"],
            "detected_language": top_language,
            "message_count": int(group["message_count"]),
            "unique_user_count": int(len(group["authors"])),
            "quality_score": round(group["quality_total"] / max(1, group["message_count"]), 3),
            "question": bool(group["question"]),
            "product_context": bool(group["product_context"]),
            "hashtags": sorted(group["hashtags"])[:6],
            "hint_categories": [name for name, _ in group["hint_counter"].most_common(4)],
            "content": _truncate(group["representative_content"], MAX_CANDIDATE_CONTENT_LENGTH),
        }
        item["signal_score"] = _candidate_signal_score(item)
        candidates.append(item)

    candidates.sort(
        key=lambda item: (
            -float(item["signal_score"]),
            -int(item["message_count"]),
            -int(item["unique_user_count"]),
            item["created_at"],
        )
    )
    raw_candidate_group_count = len(candidates)
    llm_candidates = [item for item in candidates if _should_keep_candidate_for_llm(item)]
    dropped_low_signal_candidates = raw_candidate_group_count - len(llm_candidates)
    if dropped_low_signal_candidates:
        filter_counts["low_signal_candidate"] += dropped_low_signal_candidates
    candidate_payload_chars = sum(_compact_item_size(item) for item in llm_candidates)

    shards = []
    current_items: list[dict[str, Any]] = []
    current_chars = 0
    for item in llm_candidates:
        item_size = _compact_item_size(item)
        exceeds_budget = current_items and (
            current_chars + item_size > shard_char_budget or len(current_items) >= shard_max_items
        )
        if exceeds_budget:
            shard_index = len(shards) + 1
            shards.append(
                {
                    "shard_id": f"{report_date_str}-shard-{shard_index:02d}",
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "stats": {
                        "candidate_count": len(current_items),
                        "message_count": sum(int(entry["message_count"]) for entry in current_items),
                        "channel_count": len({entry["channel_id"] for entry in current_items}),
                    },
                    "items": current_items,
                }
            )
            current_items = []
            current_chars = 0
        current_items.append(item)
        current_chars += item_size

    if current_items:
        shard_index = len(shards) + 1
        shards.append(
            {
                "shard_id": f"{report_date_str}-shard-{shard_index:02d}",
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "stats": {
                    "candidate_count": len(current_items),
                    "message_count": sum(int(entry["message_count"]) for entry in current_items),
                    "channel_count": len({entry["channel_id"] for entry in current_items}),
                },
                "items": current_items,
            }
        )

    report = {
        "report_date": report_date_str,
        "timezone": timezone_name,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "source_message_count": int(len(normalized)),
        "active_user_count": int(normalized["author_id"].nunique()),
        "candidate_message_count": int(candidate_message_count),
        "candidate_group_count": int(len(llm_candidates)),
        "raw_candidate_group_count": int(raw_candidate_group_count),
        "candidate_payload_chars": int(candidate_payload_chars),
        "shard_count": int(len(shards)),
        "shard_char_budget": int(shard_char_budget),
        "detected_language_breakdown": {
            str(language): int(count)
            for language, count in normalized["detected_language"].value_counts().items()
        },
        "filter_details": dict(filter_counts),
    }
    return {"report": report, "candidates": llm_candidates, "shards": shards}


def build_interval_pipeline_bundle_from_messages(
    messages: Iterable[Any],
    report_date: date | str,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
    shard_char_budget: int = DEFAULT_SHARD_CHAR_BUDGET,
    shard_max_items: int = DEFAULT_SHARD_MAX_ITEMS,
) -> dict[str, Any]:
    df = messages_to_dataframe(messages)
    return build_interval_pipeline_bundle_from_dataframe(
        df=df,
        report_date=report_date,
        timezone_name=timezone_name,
        window_start=window_start,
        window_end=window_end,
        shard_char_budget=shard_char_budget,
        shard_max_items=shard_max_items,
    )


def run_pipeline_from_dataframe(
    df: pd.DataFrame,
    report_date: date | str,
    timezone_name: str = "Asia/Shanghai",
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    generate_prompt: bool = False,
    lang: str = "zh",
) -> dict[str, Any]:
    del generate_prompt, lang
    normalized = normalize_input_dataframe(df)
    if window_start is None:
        window_start = (
            normalized["created_at"].min().to_pydatetime() if not normalized.empty else datetime.now(tz=timezone.utc)
        )
    if window_end is None:
        window_end = (
            normalized["created_at"].max().to_pydatetime() if not normalized.empty else datetime.now(tz=timezone.utc)
        )
    return build_interval_pipeline_bundle_from_dataframe(
        df=normalized,
        report_date=report_date,
        timezone_name=timezone_name,
        window_start=window_start,
        window_end=window_end,
    )


def run_pipeline(
    csv_path: str,
    target_date: str | None = None,
    output_dir: str = ".",
    generate_prompt: bool = False,
    lang: str = "zh",
) -> dict[str, Any]:
    del generate_prompt, lang
    df = pd.read_csv(csv_path)
    report_date = target_date or datetime.now(tz=timezone.utc).date().isoformat()
    result = run_pipeline_from_dataframe(df=df, report_date=report_date)
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"pipeline_bundle_{report_date}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build LLM-ready signal shards from Discord exports")
    parser.add_argument("csv_path", help="Path to CSV file")
    parser.add_argument("--date", default=None, help="Logical report date")
    parser.add_argument("--output-dir", default=".", help="Directory for pipeline bundle JSON")
    args = parser.parse_args()

    result = run_pipeline(args.csv_path, target_date=args.date, output_dir=args.output_dir)
    print(
        json.dumps(
            {
                "report": result["report"],
                "candidates": len(result["candidates"]),
                "shards": len(result["shards"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
