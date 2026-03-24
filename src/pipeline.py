#!/usr/bin/env python3
"""
Discord 泰语区聊天分析管道
=========================

支持两种入口：
1. CSV 文件入口，保留原有命令行能力
2. DataFrame / 数据库消息入口，供每日分析任务复用
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd


TIME_WINDOW_MINUTES = 60
MAX_REPRESENTATIVE_MSGS = 2
MIN_MSG_LENGTH = 5
DEDUP_SIMILARITY_THRESHOLD = 0.6
MAX_REPORT_EXCERPT_LENGTH = 140
MAX_HIGHLIGHTED_HIGH_PRIORITY_CLUSTERS = 6
MAX_HIGHLIGHTED_MEDIUM_PRIORITY_CLUSTERS = 4
MAX_EXCERPT_ITEMS = 8
MAX_NON_PRODUCT_SIGNAL_LENGTH = 220
MAX_PLAUSIBLE_SIGNAL_LENGTH = 600


FILLER_PATTERNS = [
    r"^[5๕]+$",
    r"^[HA]+$",
    r"^ค่[าะ]+$",
    r"^ครับ+$",
    r"^จ้า+$",
    r"^อ่า+$",
    r"^โอ้+$",
    r"^ว้า+[ยว]*$",
    r"^55+5*$",
    r"^<[^>]+>$",
    r"^(<[^>]+>\s*)+$",
    r"^[\d\s]+$",
]

EMOJI_PATTERN = re.compile(r"<a?:\w+:\d+>")
URL_PATTERN = re.compile(r"https?://\S+")

PRODUCT_CONTEXT_HINTS = {
    "แอป",
    "แอพ",
    "เว็บ",
    "เว็ป",
    "web",
    "ui",
    "rubii",
    "ai",
    "ระบบ",
    "โค้ด",
    "code",
    "login",
    "บัญชี",
    "ข้อความ",
    "reply",
    "ตอบ",
    "อัปเดต",
    "อัปเดท",
    "update",
    "สมัคร",
    "เติม",
    "ราคา",
    "แพง",
    "ลิงก์",
    "ลิงค์",
    "โหลด",
    "หน้าจอ",
    "หน้าต่าง",
    "ฟีเจอร์",
    "feature",
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
    "คณะ",
    "มหาลัย",
    "มหาวิทยาลัย",
    "เพื่อนสนิท",
    "นิสัย",
    "ตัวละคร",
    "คาแรค",
}

REPORT_MEDIUM_PRIORITY_CATEGORIES = {"feature_request", "ux_confusion"}

SIGNAL_CATEGORIES = {
    "bug_report": {
        "label_zh": "Bug/崩溃报告",
        "label_en": "Bug/crash report",
        "priority": 1,
        "keywords": [
            "บัค",
            "bug",
            "error",
            "ล่ม",
            "crash",
            "ค้าง",
            "แลค",
            "lag",
            "ผิดพลาด",
            "ใช้ไม่ได้",
            "เปิดไม่ได้",
            "ขัดข้อง",
            "ดับ",
            "แครช",
            "ไม่ทำงาน",
            "พัง",
            "หลุด",
        ],
        "context_required_for": {
            "หลุด": ["แอป", "ระบบ", "เกม", "คาแรค", "บท", "เซิร์ฟ", "เซิฟ"],
        },
    },
    "performance": {
        "label_zh": "性能/速度问题",
        "label_en": "Performance issue",
        "priority": 1,
        "keywords": [
            "ช้า",
            "โหลดนาน",
            "หน่วง",
            "กระตุก",
            "ตอบช้า",
            "โหลดไม่ขึ้น",
            "รอนาน",
            "หมุน",
            "loading",
            "slow",
            "กินแรม",
            "ร้อน",
        ],
        "context_required_for": {
            "ช้า": ["ตอบ", "โหลด", "แอป", "ระบบ", "เซิร์ฟ", "อัปเดท", "อัปเดต"],
        },
    },
    "update_issue": {
        "label_zh": "更新/安装问题",
        "label_en": "Update/install issue",
        "priority": 1,
        "keywords": [
            "อัปเดท",
            "อัปเดต",
            "update",
            "เวอร์ชั่น",
            "เวอร์ชัน",
            "version",
            "กุเกิ้ล",
            "google play",
            "play store",
            "app store",
            "แอนดรอย",
            "android",
            "ios",
            "ไอโอเอส",
            "ติดตั้ง",
            "install",
            "ดาวน์โหลด",
        ],
        "context_required_for": {},
    },
    "ai_quality": {
        "label_zh": "AI回复质量问题",
        "label_en": "AI response quality",
        "priority": 1,
        "keywords": [
            "ตอบแปลก",
            "เว้นวรรค",
            "ตอบผิด",
            "ตอบซ้ำ",
            "ไม่เข้าใจ",
            "ตอบไม่ตรง",
            "แปลกๆ",
            "พิมพ์แปลก",
            "ภาษาแปลก",
            "หลุดบท",
            "ออกบท",
            "ตอบวน",
            "จำไม่ได้",
            "หลุดคาแรค",
            "ไม่ตรง",
        ],
        "context_required_for": {},
    },
    "payment": {
        "label_zh": "付费/价格相关",
        "label_en": "Payment/pricing",
        "priority": 1,
        "keywords": [
            "จ่าย",
            "ราคา",
            "แพง",
            "ฟรี",
            "สมัคร",
            "subscribe",
            "premium",
            "พรีเมี่ยม",
            "เติม",
            "โปรโมชั่น",
            "ส่วนลด",
            "คูปอง",
            "coin",
            "gem",
            "เพชร",
            "เหรียญ",
        ],
        "context_required_for": {
            "ตังค์": ["จ่าย", "สมัคร", "เติม", "ซื้อ", "แพง", "ราคา"],
            "เงิน": ["จ่าย", "สมัคร", "เติม", "ซื้อ", "แพง", "ราคา"],
        },
    },
    "feature_request": {
        "label_zh": "功能需求/建议",
        "label_en": "Feature request",
        "priority": 2,
        "keywords": [
            "อยากให้",
            "น่าจะมี",
            "ควรจะ",
            "เพิ่ม",
            "ฟีเจอร์",
            "feature",
            "ปรับ",
            "พัฒนา",
            "ทำได้ไหม",
            "มีแพลน",
            "ต้องการ",
            "เสนอ",
            "suggest",
            "request",
            "อยากได้",
            "ขอเพิ่ม",
        ],
        "context_required_for": {
            "เพิ่ม": ["ฟีเจอร์", "ระบบ", "โหมด", "ตัวเลือก", "ฟังก์ชัน"],
            "ปรับ": ["ระบบ", "แอป", "ฟีเจอร์", "UI", "หน้า"],
        },
    },
    "ux_confusion": {
        "label_zh": "UX困惑(不会用)",
        "label_en": "UX confusion",
        "priority": 2,
        "keywords": [
            "กดตรงไหน",
            "ทำยังไง",
            "หาไม่เจอ",
            "สับสน",
            "กดยังไง",
            "อยู่ตรงไหน",
            "ไม่เจอ",
            "กดไม่ถูก",
            "ไม่รู้กด",
            "วิธี",
            "สอนหน่อย",
            "ช่วยบอก",
        ],
        "context_required_for": {},
    },
    "content_feedback": {
        "label_zh": "内容/角色反馈",
        "label_en": "Content feedback",
        "priority": 2,
        "keywords": [
            "ตัวละคร",
            "คาแรค",
            "character",
            "เนื้อเรื่อง",
            "สตอรี่",
            "story",
            "อีเวนท์",
            "event",
            "การ์ด",
            "card",
            "สกิน",
            "skin",
        ],
        "context_required_for": {},
    },
    "positive_feedback": {
        "label_zh": "正面反馈",
        "label_en": "Positive feedback",
        "priority": 3,
        "keywords": [
            "เริ่ด",
            "สุดยอด",
            "ดีมาก",
            "ชอบมาก",
            "เพิฏ",
            "เพิ้ด",
            "สนุกมาก",
            "เจ๋ง",
            "โคตรดี",
            "ยอดเยี่ยม",
        ],
        "context_required_for": {},
    },
    "backend_dev": {
        "label_zh": "开发团队动态",
        "label_en": "Backend/dev activity",
        "priority": 3,
        "keywords": [
            "แก้หลังบ้าน",
            "เทส",
            "test",
            "deploy",
            "เซิร์ฟ",
            "server",
            "แก้ใหม่",
            "maintenance",
            "ปิดปรับปรุง",
            "hotfix",
            "patch",
        ],
        "context_required_for": {},
    },
}

CATEGORY_LABELS_TH = {
    "bug_report": "รายงานบั๊ก/แครช",
    "performance": "ปัญหาด้านประสิทธิภาพ",
    "update_issue": "ปัญหาการอัปเดต/ติดตั้ง",
    "ai_quality": "ปัญหาคุณภาพการตอบของ AI",
    "payment": "ประเด็นการชำระเงิน/ราคา",
    "feature_request": "คำขอฟีเจอร์/ข้อเสนอแนะ",
    "ux_confusion": "ความสับสนในการใช้งาน",
    "content_feedback": "ฟีดแบ็กด้านคอนเทนต์/ตัวละคร",
    "positive_feedback": "ฟีดแบ็กเชิงบวก",
    "backend_dev": "ความเคลื่อนไหวของทีมพัฒนา",
}

FALSE_POSITIVE_WORDS = {
    "ดับเบิ้ล": "ดับ",
    "ดับเบิล": "ดับ",
    "ลงทัณฑ์": "ล่ม",
    "ลงทะเบียน": "ล่ม",
}


def _empty_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "message_id",
            "author_id",
            "content",
            "created_at",
            "is_target_language",
            "quality_score",
        ]
    )


def normalize_input_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_dataframe()

    normalized = df.copy()
    if "is_target_language" not in normalized.columns:
        if "is_thai" in normalized.columns:
            normalized["is_target_language"] = normalized["is_thai"]
        else:
            normalized["is_target_language"] = False

    if "created_at" not in normalized.columns:
        normalized["created_at"] = pd.Timestamp.now(tz="UTC")

    normalized["created_at"] = pd.to_datetime(normalized["created_at"], utc=True, errors="coerce")
    normalized["created_at"] = normalized["created_at"].fillna(pd.Timestamp.now(tz="UTC"))

    if "author_id" not in normalized.columns:
        normalized["author_id"] = "unknown"
    normalized["author_id"] = normalized["author_id"].fillna("unknown").astype(str)

    if "content" not in normalized.columns:
        normalized["content"] = ""
    normalized["content"] = normalized["content"].fillna("").astype(str)

    if "quality_score" not in normalized.columns:
        normalized["quality_score"] = 1.0
    normalized["quality_score"] = pd.to_numeric(normalized["quality_score"], errors="coerce").fillna(1.0)

    normalized["is_target_language"] = normalized["is_target_language"].fillna(False).astype(bool)
    return normalized


def messages_to_dataframe(messages: Iterable[Any]) -> pd.DataFrame:
    rows = []
    for message in messages:
        rows.append(
            {
                "message_id": getattr(message, "message_id", None),
                "author_id": getattr(message, "author_id", "unknown"),
                "content": getattr(message, "content", ""),
                "created_at": getattr(message, "created_at", None),
                "is_target_language": getattr(message, "is_target_language", False),
                "quality_score": getattr(message, "quality_score", 1.0),
            }
        )
    return normalize_input_dataframe(pd.DataFrame(rows))


def has_product_context(content_lower: str) -> bool:
    return any(hint in content_lower for hint in PRODUCT_CONTEXT_HINTS)


def looks_like_longform_story(content: str) -> bool:
    content_lower = content.lower()
    long_enough = len(content_lower) >= MAX_NON_PRODUCT_SIGNAL_LENGTH
    story_hits = sum(1 for hint in LONGFORM_STORY_HINTS if hint in content_lower)
    has_many_lines = content_lower.count("\n") >= 3
    quoted_profile = content_lower.count('"') >= 2 and story_hits >= 1
    return long_enough and (story_hits >= 2 or has_many_lines or quoted_profile) and not has_product_context(content_lower)


def is_noise(content: str, is_target_language: bool, quality_score: float = 1.0) -> tuple[bool, str]:
    if pd.isna(content) or not content.strip():
        return True, "empty"
    if not is_target_language:
        return True, "non_target_language"

    cleaned = EMOJI_PATTERN.sub("", content).strip()
    cleaned = URL_PATTERN.sub("", cleaned).strip()

    if len(cleaned) < MIN_MSG_LENGTH:
        return True, "too_short"
    if len(cleaned) > MAX_PLAUSIBLE_SIGNAL_LENGTH and not has_product_context(cleaned.lower()):
        return True, "too_long_non_product"
    if looks_like_longform_story(cleaned):
        return True, "longform_story"
    if quality_score < 0.35:
        return True, "low_quality"

    for pattern in FILLER_PATTERNS:
        if re.match(pattern, cleaned, re.IGNORECASE):
            return True, "filler"
    return False, ""


def stage1_filter(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    stats = {
        "input_count": len(df),
        "filtered_reasons": Counter(),
    }

    keep_mask = []
    for _, row in df.iterrows():
        noise, reason = is_noise(
            str(row.get("content", "")),
            bool(row.get("is_target_language", False)),
            float(row.get("quality_score", 1.0)),
        )
        if noise:
            stats["filtered_reasons"][reason] += 1
        keep_mask.append(not noise)

    filtered = df[keep_mask].copy()
    stats["output_count"] = len(filtered)
    stats["noise_ratio"] = 1 - len(filtered) / len(df) if len(df) > 0 else 0
    stats["filtered_reasons"] = dict(stats["filtered_reasons"])
    return filtered, stats


def check_context(content_lower: str, keyword: str, category_config: dict[str, Any]) -> bool:
    for false_word, eaten_kw in FALSE_POSITIVE_WORDS.items():
        if eaten_kw == keyword and false_word in content_lower:
            return False

    context_map = category_config.get("context_required_for", {})
    if keyword not in context_map:
        return True

    context_words = context_map[keyword]
    return any(cw in content_lower for cw in context_words)


def stage2_classify(df: pd.DataFrame) -> tuple[list[dict[str, Any]], int]:
    classified: list[dict[str, Any]] = []
    uncategorized = 0

    for _, row in df.iterrows():
        content = str(row.get("content", ""))
        content_lower = content.lower()
        matched_categories = []

        for cat_name, cat_config in SIGNAL_CATEGORIES.items():
            for keyword in cat_config["keywords"]:
                if keyword in content_lower and check_context(content_lower, keyword, cat_config):
                    matched_categories.append(cat_name)
                    break

        if matched_categories:
            classified.append(
                {
                    "time": str(row.get("created_at", "")),
                    "author_id": str(row.get("author_id", "")),
                    "content": content[:300],
                    "has_product_context": has_product_context(content_lower),
                    "categories": matched_categories,
                    "primary_category": matched_categories[0],
                }
            )
        else:
            uncategorized += 1

    return classified, uncategorized


def simple_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0

    def ngrams(value: str, size: int = 3) -> set[str]:
        if len(value) < size:
            return {value}
        return {value[i : i + size] for i in range(len(value) - size + 1)}

    a_set = ngrams(a)
    b_set = ngrams(b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)


def stage3_aggregate(classified_msgs: list[dict[str, Any]], window_minutes: int = TIME_WINDOW_MINUTES) -> list[dict[str, Any]]:
    if not classified_msgs:
        return []

    for msg in classified_msgs:
        try:
            msg["_dt"] = pd.to_datetime(msg["time"], utc=True)
        except Exception:
            msg["_dt"] = pd.Timestamp.now(tz="UTC")

    classified_msgs.sort(key=lambda item: item["_dt"])
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for msg in classified_msgs:
        window_start = msg["_dt"].floor(f"{window_minutes}min")
        for category in msg["categories"]:
            key = (str(window_start), category)
            buckets[key].append(msg)

    clusters: list[dict[str, Any]] = []
    for (window, category), msgs in buckets.items():
        unique_msgs = []
        for msg in msgs:
            is_dup = any(
                simple_similarity(msg["content"], existing["content"]) > DEDUP_SIMILARITY_THRESHOLD
                for existing in unique_msgs
            )
            if not is_dup:
                unique_msgs.append(msg)

        unique_msgs.sort(key=lambda item: len(item["content"]), reverse=True)
        representatives = unique_msgs[:MAX_REPRESENTATIVE_MSGS]
        cat_config = SIGNAL_CATEGORIES.get(category, {})
        clusters.append(
            {
                "time_window": window,
                "category": category,
                "label_zh": cat_config.get("label_zh", category),
                "label_en": cat_config.get("label_en", category),
                "label_th": CATEGORY_LABELS_TH.get(category, category),
                "priority": cat_config.get("priority", 9),
                "total_count": len(msgs),
                "unique_count": len(unique_msgs),
                "representative_messages": [
                    {"content": m["content"][:MAX_REPORT_EXCERPT_LENGTH], "author": m["author_id"][-4:]}
                    for m in representatives
                ],
            }
        )

    clusters.sort(key=lambda item: (item["priority"], item["time_window"]))
    return clusters


def stage4_generate_llm_prompt(clusters: list[dict[str, Any]], stats: dict[str, Any], lang: str = "zh") -> str:
    if lang == "zh":
        return _prompt_zh(clusters, stats)
    return _prompt_en(clusters, stats)


def _prompt_zh(clusters: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    lines = [
        "你是一个产品分析专家。以下是从 Discord 泰语社区聊天中提取的结构化信号摘要。",
        "这些数据已经过过滤和分类，你看到的每一条都是有效信号。",
        "",
        f"数据概况：原始消息 {stats.get('source_message_count', '?')} 条，"
        f"目标语言消息 {stats.get('target_message_count', '?')} 条，"
        f"过滤后 {stats.get('after_filter', '?')} 条，"
        f"分类为信号 {stats.get('signal_count', '?')} 条，"
        f"聚合为 {len(clusters)} 个聚类。",
        "",
        "请用中文总结今日用户讨论动向、紧急问题、优化方向和整体情绪。",
        "",
        "===== 以下是今日信号聚类 =====",
        "",
    ]
    for cluster in clusters:
        lines.append(f"[{cluster['time_window']}] {cluster['label_zh']} ({cluster['total_count']}条)")
        for msg in cluster["representative_messages"]:
            lines.append(f"  @{msg['author']}: {msg['content']}")
        lines.append("")
    return "\n".join(lines)


def _prompt_en(clusters: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    lines = [
        "You are a product analyst. Below is a structured signal summary extracted from a Discord Thai community chat.",
        "",
        f"Overview: {stats.get('source_message_count', '?')} total messages, "
        f"{stats.get('target_message_count', '?')} target-language messages, "
        f"{stats.get('signal_count', '?')} signals, {len(clusters)} clusters.",
        "",
        "===== Today's signal clusters =====",
        "",
    ]
    for cluster in clusters:
        lines.append(f"[{cluster['time_window']}] {cluster['label_en']} ({cluster['total_count']} msgs)")
        for msg in cluster["representative_messages"]:
            lines.append(f"  @{msg['author']}: {msg['content']}")
        lines.append("")
    return "\n".join(lines)


def _to_local_series(series: pd.Series, timezone_name: str) -> pd.Series:
    if series.empty:
        return series
    converted = pd.to_datetime(series, utc=True, errors="coerce")
    return converted.dt.tz_convert(timezone_name)


def infer_sentiment(category_distribution: dict[str, int]) -> dict[str, str]:
    negative_categories = {"bug_report", "performance", "update_issue", "ai_quality", "payment"}
    positive_categories = {"positive_feedback"}

    negative_score = sum(category_distribution.get(category, 0) for category in negative_categories)
    positive_score = sum(category_distribution.get(category, 0) for category in positive_categories)

    if negative_score == 0 and positive_score == 0:
        return {"score": "neutral", "reason": "ไม่มีสัญญาณเพียงพอสำหรับการประเมินอารมณ์ผู้ใช้"}
    if negative_score >= max(2, positive_score * 2):
        return {"score": "negative", "reason": "สัญญาณเชิงลบและประเด็นปัญหามีสัดส่วนสูงกว่าฟีดแบ็กเชิงบวกอย่างชัดเจน"}
    if positive_score > negative_score:
        return {"score": "positive", "reason": "ฟีดแบ็กเชิงบวกมีมากกว่าประเด็นปัญหาที่ถูกยกขึ้นมา"}
    return {"score": "neutral", "reason": "มีทั้งคำชมและปัญหาในสัดส่วนใกล้เคียงกัน"}


def _cluster_sort_key(cluster: dict[str, Any]) -> tuple[int, str]:
    return (-int(cluster["total_count"]), str(cluster["time_window"]))


def select_report_clusters(clusters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    high_priority = sorted((c for c in clusters if c["priority"] == 1), key=_cluster_sort_key)
    medium_priority = sorted(
        (c for c in clusters if c["priority"] == 2 and c["category"] in REPORT_MEDIUM_PRIORITY_CATEGORIES),
        key=_cluster_sort_key,
    )
    return (
        high_priority[:MAX_HIGHLIGHTED_HIGH_PRIORITY_CLUSTERS],
        medium_priority[:MAX_HIGHLIGHTED_MEDIUM_PRIORITY_CLUSTERS],
    )


def build_excerpt_items(clusters: list[dict[str, Any]], limit: int = MAX_EXCERPT_ITEMS) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for cluster in clusters:
        for msg in cluster["representative_messages"]:
            items.append(
                {
                    "category": cluster["label_th"],
                    "time_window": cluster["time_window"],
                    "author": msg["author"],
                    "content": msg["content"],
                }
            )
            if len(items) >= limit:
                return items
    return items


def generate_rule_based_report(
    df_original: pd.DataFrame,
    df_target: pd.DataFrame,
    df_filtered: pd.DataFrame,
    classified_msgs: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    stats: dict[str, Any],
    report_date: str,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    activity: dict[str, Any] = {}
    if not df_original.empty:
        timestamps_local = _to_local_series(df_original["created_at"], timezone_name)
        activity = {
            "total_messages": int(len(df_original)),
            "target_language_messages": int(len(df_target)),
            "unique_authors": int(df_original["author_id"].nunique()),
            "time_range": f"{timestamps_local.min().isoformat()} ~ {timestamps_local.max().isoformat()}",
            "peak_hours": {int(hour): int(count) for hour, count in timestamps_local.dt.hour.value_counts().nlargest(3).items()},
        }

    category_counts: Counter[str] = Counter()
    for msg in classified_msgs:
        for category in msg["categories"]:
            category_counts[category] += 1

    sentiment = infer_sentiment(dict(category_counts))
    selected_high_priority, selected_medium_priority = select_report_clusters(clusters)
    report = {
        "report_date": report_date,
        "timezone": timezone_name,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_message_count": int(len(df_original)),
        "target_message_count": int(len(df_target)),
        "pipeline_stats": stats,
        "activity": activity,
        "category_distribution": dict(category_counts),
        "sentiment": sentiment,
        "all_clusters": clusters,
        "high_priority_clusters": [cluster for cluster in clusters if cluster["priority"] == 1],
        "medium_priority_clusters": [cluster for cluster in clusters if cluster["priority"] == 2],
        "low_priority_clusters": [cluster for cluster in clusters if cluster["priority"] >= 3],
        "selected_high_priority_clusters": selected_high_priority,
        "selected_medium_priority_clusters": selected_medium_priority,
        "report_clusters": selected_high_priority + selected_medium_priority,
        "excerpt_items": build_excerpt_items(selected_high_priority + selected_medium_priority),
        "total_clusters": len(clusters),
    }
    return report


def _format_cluster_lines(clusters: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for cluster in clusters:
        lines.append(
            f"### {cluster['label_th']} | {cluster['time_window']} | {cluster['total_count']} ข้อความ ({cluster['unique_count']} ไม่ซ้ำ)"
        )
        for msg in cluster["representative_messages"]:
            lines.append(f"- @{msg['author']}: {msg['content']}")
        lines.append("")
    return lines


def _format_excerpt_lines(excerpts: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for item in excerpts:
        lines.append(f"- [{item['category']} | {item['time_window']}] @{item['author']}: {item['content']}")
    return lines


def format_thai_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# รายงานประจำวัน",
        "",
        f"- วันที่รายงาน: {report['report_date']}",
        f"- เขตเวลา: {report['timezone']}",
        f"- ช่วงเวลาเก็บข้อมูล: {report['window_start']} ถึง {report['window_end']}",
        "",
        "## ภาพรวมของวัน",
        "",
    ]

    if report["source_message_count"] == 0:
        lines.extend(
            [
                "- วันนี้ไม่มีข้อความใหม่ที่ถูกเก็บเข้าระบบ",
                "- ไม่มีสัญญาณจากผู้ใช้ให้สรุปเพิ่มเติม",
                "",
                "## ประเด็นสำคัญ",
                "",
                "- ไม่มีประเด็นสำคัญในช่วงเวลานี้",
                "",
                "## โอกาสด้านผลิตภัณฑ์",
                "",
                "- ไม่มีข้อมูลเพียงพอสำหรับเสนอแนวทางเพิ่มเติม",
                "",
                "## อารมณ์ผู้ใช้",
                "",
                "- เป็นกลาง เนื่องจากไม่มีข้อความใหม่",
                "",
                "## ตัวอย่างข้อความ",
                "",
                "- ไม่มีข้อความตัวอย่าง",
                "",
                "## ข้อมูลเชิงสถิติ",
                "",
                "- จำนวนข้อความทั้งหมด: 0",
                "- จำนวนข้อความภาษาเป้าหมาย: 0",
            ]
        )
        return "\n".join(lines)

    activity = report.get("activity", {})
    peak_hours = activity.get("peak_hours", {})
    peak_hours_text = ", ".join(f"{hour}:00 ({count})" for hour, count in peak_hours.items()) if peak_hours else "ไม่มีข้อมูล"
    lines.extend(
        [
            f"- ข้อความทั้งหมดที่เก็บได้: {report['source_message_count']}",
            f"- ข้อความภาษาเป้าหมาย: {report['target_message_count']}",
            f"- จำนวนผู้ใช้ที่มีความเคลื่อนไหว: {activity.get('unique_authors', 0)}",
            f"- ช่วงเวลาที่คึกคักที่สุด: {peak_hours_text}",
            "",
            "## ประเด็นสำคัญ",
            "",
        ]
    )

    high_priority_clusters = report.get("selected_high_priority_clusters", [])
    if high_priority_clusters:
        lines.extend(_format_cluster_lines(high_priority_clusters))
    else:
        lines.append("- ไม่พบประเด็นเร่งด่วนจากข้อความภาษาเป้าหมาย")
        lines.append("")

    lines.extend(["## โอกาสด้านผลิตภัณฑ์", ""])
    opportunity_clusters = report.get("selected_medium_priority_clusters", [])
    if opportunity_clusters:
        lines.extend(_format_cluster_lines(opportunity_clusters))
    else:
        lines.append("- ยังไม่พบข้อเสนอเชิงผลิตภัณฑ์ที่ชัดเจนในวันนี้")
        lines.append("")

    sentiment = report.get("sentiment", {})
    lines.extend(
        [
            "## อารมณ์ผู้ใช้",
            "",
            f"- ระดับอารมณ์โดยรวม: {sentiment.get('score', 'neutral')}",
            f"- เหตุผล: {sentiment.get('reason', '')}",
            "",
            "## ตัวอย่างข้อความ",
            "",
        ]
    )

    excerpt_items = report.get("excerpt_items", [])
    if excerpt_items:
        lines.extend(_format_excerpt_lines(excerpt_items))
    else:
        lines.append("- ไม่มีข้อความตัวอย่างที่ผ่านการคัดกรอง")
        lines.append("")

    category_distribution = report.get("category_distribution", {})
    lines.extend(["## ข้อมูลเชิงสถิติ", ""])
    lines.append(f"- จำนวนข้อความทั้งหมด: {report['source_message_count']}")
    lines.append(f"- จำนวนข้อความภาษาเป้าหมาย: {report['target_message_count']}")
    lines.append(f"- จำนวนสัญญาณหลังคัดกรอง: {report['pipeline_stats'].get('signal_count', 0)}")
    lines.append(f"- จำนวนคลัสเตอร์: {report.get('total_clusters', 0)}")
    if category_distribution:
        lines.append("- การกระจายหมวดหมู่:")
        for category, count in sorted(category_distribution.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"  - {CATEGORY_LABELS_TH.get(category, category)}: {count}")
    filter_details = report["pipeline_stats"].get("filter_details", {})
    if filter_details:
        lines.append("- รายละเอียดการกรองข้อความ:")
        for reason, count in sorted(filter_details.items()):
            lines.append(f"  - {reason}: {count}")
    return "\n".join(lines)


def build_daily_report_from_dataframe(
    df: pd.DataFrame,
    report_date: date | str,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    normalized = normalize_input_dataframe(df)
    target_df = normalized[normalized["is_target_language"]].copy()
    df_filtered, s1_stats = stage1_filter(target_df)
    classified, uncategorized = stage2_classify(df_filtered)
    clusters = stage3_aggregate(classified)

    pipeline_stats = {
        "source_message_count": int(len(normalized)),
        "target_message_count": int(len(target_df)),
        "input_count": int(len(target_df)),
        "after_filter": int(s1_stats["output_count"]),
        "noise_ratio": float(s1_stats["noise_ratio"]),
        "filter_details": s1_stats["filtered_reasons"],
        "signal_count": int(len(classified)),
        "uncategorized_count": int(uncategorized),
        "cluster_count": int(len(clusters)),
        "compression_ratio": 1 - len(clusters) / len(target_df) if len(target_df) > 0 else 0,
    }
    report = generate_rule_based_report(
        df_original=normalized,
        df_target=target_df,
        df_filtered=df_filtered,
        classified_msgs=classified,
        clusters=clusters,
        stats=pipeline_stats,
        report_date=report_date.isoformat() if isinstance(report_date, date) else str(report_date),
        timezone_name=timezone_name,
        window_start=window_start,
        window_end=window_end,
    )
    markdown = format_thai_markdown_report(report)
    return {"report": report, "markdown": markdown}


def build_daily_report_from_messages(
    messages: Iterable[Any],
    report_date: date | str,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    df = messages_to_dataframe(messages)
    return build_daily_report_from_dataframe(df, report_date, timezone_name, window_start, window_end)


def run_pipeline_from_dataframe(
    df: pd.DataFrame,
    report_date: date | str,
    timezone_name: str = "Asia/Shanghai",
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    generate_prompt: bool = False,
    lang: str = "zh",
) -> dict[str, Any]:
    normalized = normalize_input_dataframe(df)
    if window_start is None:
        window_start = normalized["created_at"].min().to_pydatetime() if not normalized.empty else datetime.now(tz=timezone.utc)
    if window_end is None:
        window_end = normalized["created_at"].max().to_pydatetime() if not normalized.empty else datetime.now(tz=timezone.utc)

    result = build_daily_report_from_dataframe(
        normalized,
        report_date=report_date,
        timezone_name=timezone_name,
        window_start=window_start,
        window_end=window_end,
    )
    if generate_prompt:
        result["llm_prompt"] = stage4_generate_llm_prompt(
            result["report"]["report_clusters"],
            result["report"]["pipeline_stats"],
            lang=lang,
        )
    return result


def run_pipeline(
    csv_path: str,
    target_date: str | None = None,
    output_dir: str = ".",
    generate_prompt: bool = False,
    lang: str = "zh",
) -> dict[str, Any]:
    os.makedirs(output_dir, exist_ok=True)

    print(f"[Load] Reading {csv_path}...")
    df = pd.read_csv(csv_path)
    df = normalize_input_dataframe(df)

    if target_date:
        target = pd.to_datetime(target_date).date()
        df = df[df["created_at"].dt.tz_convert("Asia/Shanghai").dt.date == target]

    report_date = target_date or datetime.now(tz=ZoneInfo("Asia/Shanghai")).date().isoformat()
    window_start = df["created_at"].min().to_pydatetime() if not df.empty else datetime.now(tz=timezone.utc)
    window_end = df["created_at"].max().to_pydatetime() if not df.empty else datetime.now(tz=timezone.utc)

    print(f"  -> {len(df)} messages loaded")
    result = run_pipeline_from_dataframe(
        df=df,
        report_date=report_date,
        timezone_name="Asia/Shanghai",
        window_start=window_start,
        window_end=window_end,
        generate_prompt=generate_prompt,
        lang=lang,
    )

    json_path = os.path.join(output_dir, "report.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result["report"], handle, ensure_ascii=False, indent=2)
    print(f"[Output] JSON report -> {json_path}")

    markdown_path = os.path.join(output_dir, "report.md")
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write(result["markdown"])
    print(f"[Output] Markdown report -> {markdown_path}")

    if generate_prompt:
        prompt_path = os.path.join(output_dir, "llm_prompt.txt")
        with open(prompt_path, "w", encoding="utf-8") as handle:
            handle.write(result["llm_prompt"])
        print(f"[Output] LLM prompt -> {prompt_path}")

    print("\n" + result["markdown"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord Thai Chat Analysis Pipeline")
    parser.add_argument("csv_path", help="Input CSV file path")
    parser.add_argument("-o", "--output", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("-d", "--date", default=None, help="Filter by date YYYY-MM-DD")
    parser.add_argument("--llm-prompt", action="store_true", help="Generate LLM prompt file")
    parser.add_argument("--lang", default="zh", choices=["zh", "en"], help="LLM prompt language")
    args = parser.parse_args()

    run_pipeline(
        csv_path=args.csv_path,
        target_date=args.date,
        output_dir=args.output,
        generate_prompt=args.llm_prompt,
        lang=args.lang,
    )


if __name__ == "__main__":
    main()
