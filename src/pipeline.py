#!/usr/bin/env python3
"""
Discord 泰语区聊天分析管道 v2 (Production Pipeline)
====================================================

四层压缩架构：
  Stage 1: 降噪过滤 (规则)        50,000 → ~7,500
  Stage 2: 关键词分类 (规则)       7,500 → ~1,500 信号
  Stage 3: 时间窗口聚合 (统计)     1,500 → ~150 聚类
  Stage 4: LLM 摘要 (可选)        150 → 日报

前三层完全离线，零 API 成本。第四层可选接入 LLM。

用法：
  # 基础用法：纯规则处理，输出 JSON + 文本报告
  python pipeline.py input.csv

  # 指定日期
  python pipeline.py input.csv --date 2026-03-23

  # 生成 LLM prompt（不自动调用，输出 prompt 文件让你自己喂）
  python pipeline.py input.csv --llm-prompt

  # 完整参数
  python pipeline.py input.csv -o output/ --date 2026-03-23 --llm-prompt --lang zh
"""

import pandas as pd
import json
import re
import os
import sys
import hashlib
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta


# ============================================================
# 配置
# ============================================================

# 时间窗口大小（分钟），用于 Stage 3 聚合
TIME_WINDOW_MINUTES = 60

# 每个聚类最多保留的代表性消息数
MAX_REPRESENTATIVE_MSGS = 3

# 消息最短长度（字符），低于此视为噪音
MIN_MSG_LENGTH = 5

# 相似度去重：连续消息中，同一用户发的相似内容合并
DEDUP_SIMILARITY_THRESHOLD = 0.6


# ============================================================
# Stage 1: 降噪过滤
# ============================================================

# 泰语网络用语中的"无意义词"（语气词、感叹词等）
# 如果一条消息去掉这些之后几乎为空，就是噪音
FILLER_PATTERNS = [
    r'^[5๕]+$',                    # 55555 = 哈哈哈
    r'^[HA]+$',                     # HAHAHA
    r'^ค่[าะ]+$',                   # คาา / ค่ะะ = 语气词
    r'^ครับ+$',                     # ครับ = 是的（单独）
    r'^จ้า+$',                      # 是呀
    r'^อ่า+$',                      # 啊...
    r'^โอ้+$',                      # 哦...
    r'^ว้า+[ยว]*$',                # 哇
    r'^55+5*$',                     # 555 laugh
    r'^<[^>]+>$',                   # 单个emoji <:name:id>
    r'^(<[^>]+>\s*)+$',             # 多个emoji
    r'^[\d\s]+$',                   # 纯数字
]

EMOJI_PATTERN = re.compile(r'<a?:\w+:\d+>')
URL_PATTERN = re.compile(r'https?://\S+')


def is_noise(content: str, is_thai: bool) -> tuple:
    """
    判断消息是否为噪音。
    返回 (is_noise: bool, reason: str)
    """
    if pd.isna(content) or not content.strip():
        return True, "empty"

    # 非泰语
    if not is_thai:
        return True, "non_thai"

    # 去掉emoji和URL后检查
    cleaned = EMOJI_PATTERN.sub('', content).strip()
    cleaned = URL_PATTERN.sub('', cleaned).strip()

    # 过短
    if len(cleaned) < MIN_MSG_LENGTH:
        return True, "too_short"

    # 匹配无意义模式
    for pattern in FILLER_PATTERNS:
        if re.match(pattern, cleaned, re.IGNORECASE):
            return True, "filler"

    return False, ""


def stage1_filter(df: pd.DataFrame) -> tuple:
    """
    Stage 1: 降噪过滤
    返回 (filtered_df, stats_dict)
    """
    stats = {
        "input_count": len(df),
        "filtered_reasons": Counter(),
    }

    keep_mask = []
    for _, row in df.iterrows():
        noise, reason = is_noise(str(row.get('content', '')), row.get('is_thai', False))
        if noise:
            stats["filtered_reasons"][reason] += 1
        keep_mask.append(not noise)

    filtered = df[keep_mask].copy()
    stats["output_count"] = len(filtered)
    stats["noise_ratio"] = 1 - len(filtered) / len(df) if len(df) > 0 else 0
    stats["filtered_reasons"] = dict(stats["filtered_reasons"])

    return filtered, stats


# ============================================================
# Stage 2: 关键词分类
# ============================================================

SIGNAL_CATEGORIES = {
    "bug_report": {
        "label_zh": "Bug/崩溃报告",
        "label_en": "Bug/crash report",
        "priority": 1,  # 1=最高
        "keywords": [
            'บัค', 'bug', 'error', 'ล่ม', 'crash', 'ค้าง', 'แลค', 'lag',
            'ผิดพลาด', 'ใช้ไม่ได้', 'เปิดไม่ได้', 'ขัดข้อง',
            'ดับ', 'แครช', 'ไม่ทำงาน', 'พัง', 'หลุด',
        ],
        # 上下文要求：包含以下任一词时才算（减少误匹配）
        "context_required_for": {
            'หลุด': ['แอป', 'ระบบ', 'เกม', 'คาแรค', 'บท', 'เซิร์ฟ', 'เซิฟ'],
        }
    },
    "performance": {
        "label_zh": "性能/速度问题",
        "label_en": "Performance issue",
        "priority": 1,
        "keywords": [
            'ช้า', 'โหลดนาน', 'หน่วง', 'กระตุก', 'ตอบช้า', 'โหลดไม่ขึ้น',
            'รอนาน', 'หมุน', 'loading', 'slow', 'กินแรม', 'ร้อน',
        ],
        "context_required_for": {
            'ช้า': ['ตอบ', 'โหลด', 'แอป', 'ระบบ', 'เซิร์ฟ', 'อัปเดท', 'อัปเดต'],
        }
    },
    "update_issue": {
        "label_zh": "更新/安装问题",
        "label_en": "Update/install issue",
        "priority": 1,
        "keywords": [
            'อัปเดท', 'อัปเดต', 'update', 'เวอร์ชั่น', 'เวอร์ชัน', 'version',
            'กุเกิ้ล', 'google play', 'play store', 'app store', 'แอนดรอย',
            'android', 'ios', 'ไอโอเอส', 'ติดตั้ง', 'install', 'ดาวน์โหลด',
        ],
        "context_required_for": {}
    },
    "ai_quality": {
        "label_zh": "AI回复质量问题",
        "label_en": "AI response quality",
        "priority": 1,
        "keywords": [
            'ตอบแปลก', 'เว้นวรรค', 'ตอบผิด', 'ตอบซ้ำ', 'ไม่เข้าใจ',
            'ตอบไม่ตรง', 'แปลกๆ', 'พิมพ์แปลก', 'ภาษาแปลก',
            'หลุดบท', 'ออกบท', 'ตอบวน', 'จำไม่ได้',
            'หลุดคาแรค', 'ไม่ตรง',
        ],
        "context_required_for": {}
    },
    "payment": {
        "label_zh": "付费/价格相关",
        "label_en": "Payment/pricing",
        "priority": 1,
        "keywords": [
            'จ่าย', 'ราคา', 'แพง', 'ฟรี', 'สมัคร', 'subscribe',
            'premium', 'พรีเมี่ยม', 'เติม', 'โปรโมชั่น', 'ส่วนลด',
            'คูปอง', 'coin', 'gem', 'เพชร', 'เหรียญ',
        ],
        "context_required_for": {
            'ตังค์': ['จ่าย', 'สมัคร', 'เติม', 'ซื้อ', 'แพง', 'ราคา'],
            'เงิน': ['จ่าย', 'สมัคร', 'เติม', 'ซื้อ', 'แพง', 'ราคา'],
        }
    },
    "feature_request": {
        "label_zh": "功能需求/建议",
        "label_en": "Feature request",
        "priority": 2,
        "keywords": [
            'อยากให้', 'น่าจะมี', 'ควรจะ', 'เพิ่ม', 'ฟีเจอร์', 'feature',
            'ปรับ', 'พัฒนา', 'ทำได้ไหม', 'มีแพลน', 'ต้องการ',
            'เสนอ', 'suggest', 'request', 'อยากได้', 'ขอเพิ่ม',
        ],
        "context_required_for": {
            'เพิ่ม': ['ฟีเจอร์', 'ระบบ', 'โหมด', 'ตัวเลือก', 'ฟังก์ชัน'],
            'ปรับ': ['ระบบ', 'แอป', 'ฟีเจอร์', 'UI', 'หน้า'],
        }
    },
    "ux_confusion": {
        "label_zh": "UX困惑(不会用)",
        "label_en": "UX confusion",
        "priority": 2,
        "keywords": [
            'กดตรงไหน', 'ทำยังไง', 'หาไม่เจอ', 'สับสน', 'กดยังไง',
            'อยู่ตรงไหน', 'ไม่เจอ', 'กดไม่ถูก', 'ไม่รู้กด',
            'วิธี', 'สอนหน่อย', 'ช่วยบอก',
        ],
        "context_required_for": {}
    },
    "content_feedback": {
        "label_zh": "内容/角色反馈",
        "label_en": "Content feedback",
        "priority": 2,
        "keywords": [
            'ตัวละคร', 'คาแรค', 'character', 'เนื้อเรื่อง',
            'สตอรี่', 'story', 'อีเวนท์', 'event',
            'การ์ด', 'card', 'สกิน', 'skin',
        ],
        "context_required_for": {}
    },
    "positive_feedback": {
        "label_zh": "正面反馈",
        "label_en": "Positive feedback",
        "priority": 3,
        "keywords": [
            'เริ่ด', 'สุดยอด', 'ดีมาก', 'ชอบมาก', 'เพิฏ', 'เพิ้ด',
            'สนุกมาก', 'เจ๋ง', 'โคตรดี', 'ยอดเยี่ยม',
        ],
        "context_required_for": {}
    },
    "backend_dev": {
        "label_zh": "开发团队动态",
        "label_en": "Backend/dev activity",
        "priority": 3,
        "keywords": [
            'แก้หลังบ้าน', 'เทส', 'test', 'deploy', 'เซิร์ฟ', 'server',
            'แก้ใหม่', 'maintenance', 'ปิดปรับปรุง', 'hotfix', 'patch',
        ],
        "context_required_for": {}
    },
}


# 已知的误匹配词：这些词包含关键词子串但含义完全不同
# key = 会被误匹配的完整词, value = 它"吃掉"了哪个关键词
FALSE_POSITIVE_WORDS = {
    'ดับเบิ้ล': 'ดับ',      # double ≠ crashed
    'ดับเบิล': 'ดับ',       # double variant
    'ลงทัณฑ์': 'ล่ม',       # punish ≠ crash (rare but appears in RP)
    'ลงทะเบียน': 'ล่ม',     # register ≠ crash
}


def check_context(content_lower: str, keyword: str, category_config: dict) -> bool:
    """
    检查关键词是否需要上下文验证。
    有些词（如 "หลุด"=掉了）在社交闲聊中也常出现，
    需要和产品相关的上下文词同时出现才算信号。
    """
    # 检查是否是误匹配词
    for false_word, eaten_kw in FALSE_POSITIVE_WORDS.items():
        if eaten_kw == keyword and false_word in content_lower:
            return False

    context_map = category_config.get("context_required_for", {})
    if keyword not in context_map:
        return True  # 不需要上下文验证，直接通过

    context_words = context_map[keyword]
    return any(cw in content_lower for cw in context_words)


def stage2_classify(df: pd.DataFrame) -> tuple:
    """
    Stage 2: 关键词分类
    返回 (classified_messages: list[dict], uncategorized_count: int)
    """
    classified = []
    uncategorized = 0

    for _, row in df.iterrows():
        content = str(row.get('content', ''))
        content_lower = content.lower()
        matched_categories = []

        for cat_name, cat_config in SIGNAL_CATEGORIES.items():
            for kw in cat_config['keywords']:
                if kw in content_lower:
                    if check_context(content_lower, kw, cat_config):
                        matched_categories.append(cat_name)
                        break  # 一个类别匹配一次就够

        if matched_categories:
            classified.append({
                'time': str(row.get('created_at', '')),
                'author_id': str(row.get('author_id', '')),
                'content': content[:300],
                'categories': matched_categories,
                'primary_category': matched_categories[0],
            })
        else:
            uncategorized += 1

    return classified, uncategorized


# ============================================================
# Stage 3: 时间窗口聚合
# ============================================================

def simple_similarity(a: str, b: str) -> float:
    """
    简单的字符级 Jaccard 相似度，用于去重。
    不需要任何NLP库。
    """
    if not a or not b:
        return 0.0
    # 用 3-gram 代替词级别（泰语没有空格分词）
    def ngrams(s, n=3):
        return set(s[i:i+n] for i in range(len(s)-n+1))

    a_set = ngrams(a)
    b_set = ngrams(b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)


def stage3_aggregate(classified_msgs: list, window_minutes: int = TIME_WINDOW_MINUTES) -> list:
    """
    Stage 3: 按 (时间窗口 × 类别) 聚合，去重，选代表性消息。
    返回 clusters 列表。
    """
    if not classified_msgs:
        return []

    # 解析时间
    for msg in classified_msgs:
        try:
            msg['_dt'] = pd.to_datetime(msg['time'])
        except Exception:
            msg['_dt'] = pd.Timestamp.now()

    # 按时间排序
    classified_msgs.sort(key=lambda x: x['_dt'])

    # 按 (时间窗口, 类别) 分桶
    buckets = defaultdict(list)
    for msg in classified_msgs:
        window_start = msg['_dt'].floor(f'{window_minutes}min')
        for cat in msg['categories']:
            key = (str(window_start), cat)
            buckets[key].append(msg)

    # 对每个桶：去重 + 选代表性消息
    clusters = []
    for (window, category), msgs in buckets.items():
        # 去重：相似内容只保留第一条
        unique_msgs = []
        for msg in msgs:
            is_dup = False
            for existing in unique_msgs:
                if simple_similarity(msg['content'], existing['content']) > DEDUP_SIMILARITY_THRESHOLD:
                    is_dup = True
                    break
            if not is_dup:
                unique_msgs.append(msg)

        # 选代表性消息（按长度降序，长消息通常信息量更大）
        unique_msgs.sort(key=lambda x: len(x['content']), reverse=True)
        representatives = unique_msgs[:MAX_REPRESENTATIVE_MSGS]

        cat_config = SIGNAL_CATEGORIES.get(category, {})
        clusters.append({
            'time_window': window,
            'category': category,
            'label_zh': cat_config.get('label_zh', category),
            'label_en': cat_config.get('label_en', category),
            'priority': cat_config.get('priority', 9),
            'total_count': len(msgs),
            'unique_count': len(unique_msgs),
            'representative_messages': [
                {'content': m['content'][:200], 'author': m['author_id'][-4:]}
                for m in representatives
            ],
        })

    # 按优先级排序
    clusters.sort(key=lambda x: (x['priority'], x['time_window']))
    return clusters


# ============================================================
# Stage 4: LLM Prompt 生成
# ============================================================

def stage4_generate_llm_prompt(clusters: list, stats: dict, lang: str = 'zh') -> str:
    """
    Stage 4: 将聚类结果压缩成 LLM 可处理的 prompt。
    不调用 LLM，只生成 prompt 文本，交给调用方去处理。
    """
    if lang == 'zh':
        return _prompt_zh(clusters, stats)
    else:
        return _prompt_en(clusters, stats)


def _prompt_zh(clusters, stats):
    lines = [
        "你是一个产品分析专家。以下是从 Discord 泰语社区聊天中提取的结构化信号摘要。",
        "这些数据已经过四层过滤和分类，你看到的每一条都是有效信号。",
        "",
        f"数据概况：原始消息 {stats.get('input_count', '?')} 条，"
        f"过滤后 {stats.get('after_filter', '?')} 条，"
        f"分类为信号 {stats.get('signal_count', '?')} 条，"
        f"聚合为 {len(clusters)} 个聚类。",
        "",
        "请完成以下任务：",
        "1. 用中文总结今日用户讨论的主要动向（3-5个要点）",
        "2. 列出需要产品/技术团队立即关注的问题（如有）",
        "3. 列出可能的功能优化方向（如有）",
        "4. 评估整体用户情绪（正面/中性/负面）并说明依据",
        "",
        "输出格式要求：用 JSON，结构如下：",
        '{"daily_summary": "...", "urgent_issues": [...], "optimization_ideas": [...], "sentiment": {"score": "positive/neutral/negative", "reason": "..."}}',
        "",
        "===== 以下是今日信号聚类 =====",
        "",
    ]

    priority_labels = {1: "🔴 高优先级", 2: "🟡 中优先级", 3: "🟢 低优先级"}
    current_priority = None

    for cluster in clusters:
        p = cluster['priority']
        if p != current_priority:
            current_priority = p
            lines.append(f"\n--- {priority_labels.get(p, f'优先级{p}')} ---\n")

        lines.append(f"[{cluster['time_window']}] {cluster['label_zh']} "
                     f"(共{cluster['total_count']}条, 去重后{cluster['unique_count']}条)")
        for msg in cluster['representative_messages']:
            lines.append(f"  @{msg['author']}: {msg['content']}")
        lines.append("")

    return "\n".join(lines)


def _prompt_en(clusters, stats):
    lines = [
        "You are a product analyst. Below is a structured signal summary extracted from a Discord Thai community chat.",
        "This data has been through 4 layers of filtering and classification. Every item you see is a valid signal.",
        "",
        f"Data overview: {stats.get('input_count', '?')} raw messages, "
        f"{stats.get('after_filter', '?')} after noise filter, "
        f"{stats.get('signal_count', '?')} classified as signals, "
        f"aggregated into {len(clusters)} clusters.",
        "",
        "Tasks:",
        "1. Summarize the main discussion trends today (3-5 bullet points)",
        "2. List issues requiring immediate product/tech attention (if any)",
        "3. List potential feature optimization directions (if any)",
        "4. Assess overall user sentiment (positive/neutral/negative) with evidence",
        "",
        'Output as JSON: {"daily_summary": "...", "urgent_issues": [...], "optimization_ideas": [...], "sentiment": {"score": "...", "reason": "..."}}',
        "",
        "===== Today's signal clusters =====",
        "",
    ]

    priority_labels = {1: "HIGH", 2: "MEDIUM", 3: "LOW"}
    current_priority = None

    for cluster in clusters:
        p = cluster['priority']
        if p != current_priority:
            current_priority = p
            lines.append(f"\n--- {priority_labels.get(p, f'P{p}')} ---\n")

        lines.append(f"[{cluster['time_window']}] {cluster['label_en']} "
                     f"({cluster['total_count']} msgs, {cluster['unique_count']} unique)")
        for msg in cluster['representative_messages']:
            lines.append(f"  @{msg['author']}: {msg['content']}")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# 统计报告生成（纯规则，不依赖 LLM）
# ============================================================

def generate_rule_based_report(df_original, df_filtered, classified_msgs, clusters, stats):
    """
    纯规则生成的结构化报告。即使不用 LLM，这个报告本身也有价值。
    """
    # 活跃度
    thai_df = df_original[df_original.get('is_thai', df_original.columns[0]) == True] if 'is_thai' in df_original.columns else df_original
    activity = {}
    if 'created_at' in df_original.columns:
        timestamps = pd.to_datetime(df_original['created_at'])
        activity = {
            'total_messages': len(df_original),
            'unique_authors': df_original['author_id'].nunique() if 'author_id' in df_original.columns else 0,
            'time_range': f"{timestamps.min()} ~ {timestamps.max()}",
            'peak_hours': timestamps.dt.hour.value_counts().nlargest(3).to_dict(),
        }

    # 按类别统计
    category_counts = Counter()
    for msg in classified_msgs:
        for cat in msg['categories']:
            category_counts[cat] += 1

    # 高优先级聚类
    high_priority = [c for c in clusters if c['priority'] == 1]
    medium_priority = [c for c in clusters if c['priority'] == 2]

    report = {
        'report_date': str(datetime.now().date()),
        'generated_at': str(datetime.now()),
        'pipeline_stats': stats,
        'activity': activity,
        'category_distribution': dict(category_counts),
        'high_priority_clusters': high_priority,
        'medium_priority_clusters': medium_priority,
        'total_clusters': len(clusters),
    }

    return report


def format_text_report(report: dict) -> str:
    """人可读的文本报告"""
    lines = []
    lines.append("=" * 56)
    lines.append("  Discord 泰语区日报")
    lines.append(f"  {report['report_date']}")
    lines.append("=" * 56)

    ps = report.get('pipeline_stats', {})
    lines.append(f"\n📊 管道统计")
    lines.append(f"  原始消息: {ps.get('input_count', '?')}")
    lines.append(f"  降噪后:   {ps.get('after_filter', '?')} "
                 f"(过滤 {ps.get('noise_ratio', 0):.0%})")
    lines.append(f"  信号数:   {ps.get('signal_count', '?')}")
    lines.append(f"  聚类数:   {report.get('total_clusters', '?')}")

    act = report.get('activity', {})
    if act:
        lines.append(f"\n👥 活跃度")
        lines.append(f"  用户数: {act.get('unique_authors', '?')}")
        peak = act.get('peak_hours', {})
        if peak:
            peak_str = ", ".join(f"{h}:00({c})" for h, c in peak.items())
            lines.append(f"  高峰: {peak_str}")

    # 类别分布
    dist = report.get('category_distribution', {})
    if dist:
        lines.append(f"\n📋 信号分布")
        for cat, count in sorted(dist.items(), key=lambda x: -x[1]):
            label = SIGNAL_CATEGORIES.get(cat, {}).get('label_zh', cat)
            lines.append(f"  {label}: {count}")

    # 高优先级
    high = report.get('high_priority_clusters', [])
    if high:
        lines.append(f"\n🔴 高优先级 ({len(high)}个聚类)")
        lines.append("-" * 50)
        for c in high:
            lines.append(f"\n  [{c['time_window'][:16]}] "
                        f"{c['label_zh']} ({c['total_count']}条)")
            for msg in c['representative_messages']:
                lines.append(f"    @{msg['author']}: "
                            f"{msg['content'][:80]}")

    # 中优先级
    medium = report.get('medium_priority_clusters', [])
    if medium:
        lines.append(f"\n🟡 中优先级 ({len(medium)}个聚类)")
        lines.append("-" * 50)
        for c in medium:
            lines.append(f"\n  [{c['time_window'][:16]}] "
                        f"{c['label_zh']} ({c['total_count']}条)")
            for msg in c['representative_messages']:
                lines.append(f"    @{msg['author']}: "
                            f"{msg['content'][:80]}")

    lines.append("\n" + "=" * 56)
    return "\n".join(lines)


# ============================================================
# 主流程
# ============================================================

def run_pipeline(csv_path: str, target_date: str = None,
                 output_dir: str = '.', generate_prompt: bool = False,
                 lang: str = 'zh'):
    """
    执行完整四层管道。
    """
    os.makedirs(output_dir, exist_ok=True)

    # ---- Load ----
    print(f"[Load] Reading {csv_path}...")
    df = pd.read_csv(csv_path)
    if 'created_at' in df.columns:
        df['created_at'] = pd.to_datetime(df['created_at'])
        if target_date:
            target = pd.to_datetime(target_date).date()
            df = df[df['created_at'].dt.date == target]
    print(f"  → {len(df)} messages loaded")

    # ---- Stage 1 ----
    print(f"[Stage 1] Noise filtering...")
    df_filtered, s1_stats = stage1_filter(df)
    print(f"  → {s1_stats['output_count']} kept "
          f"(filtered {s1_stats['noise_ratio']:.1%})")
    print(f"  → reasons: {s1_stats['filtered_reasons']}")

    # ---- Stage 2 ----
    print(f"[Stage 2] Keyword classification...")
    classified, uncategorized = stage2_classify(df_filtered)
    print(f"  → {len(classified)} signals, "
          f"{uncategorized} uncategorized (social chat)")

    # ---- Stage 3 ----
    print(f"[Stage 3] Time-window aggregation...")
    clusters = stage3_aggregate(classified)
    print(f"  → {len(clusters)} clusters")

    # ---- Pipeline stats ----
    pipeline_stats = {
        'input_count': len(df),
        'after_filter': s1_stats['output_count'],
        'noise_ratio': s1_stats['noise_ratio'],
        'filter_details': s1_stats['filtered_reasons'],
        'signal_count': len(classified),
        'uncategorized_count': uncategorized,
        'cluster_count': len(clusters),
        'compression_ratio': 1 - len(clusters) / len(df) if len(df) > 0 else 0,
    }

    # ---- Generate reports ----
    report = generate_rule_based_report(
        df, df_filtered, classified, clusters, pipeline_stats)

    # JSON report
    json_path = os.path.join(output_dir, 'report.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[Output] JSON report → {json_path}")

    # Text report
    text_report = format_text_report(report)
    text_path = os.path.join(output_dir, 'report.txt')
    with open(text_path, 'w', encoding='utf-8') as f:
        f.write(text_report)
    print(f"[Output] Text report → {text_path}")

    # ---- Stage 4 (optional): LLM prompt ----
    if generate_prompt:
        print(f"[Stage 4] Generating LLM prompt ({lang})...")
        prompt = stage4_generate_llm_prompt(clusters, pipeline_stats, lang)
        prompt_path = os.path.join(output_dir, 'llm_prompt.txt')
        with open(prompt_path, 'w', encoding='utf-8') as f:
            f.write(prompt)

        # 估算 token 数（粗略：1 token ≈ 4 字符 for 中文/泰语）
        est_tokens = len(prompt) // 2
        print(f"  → prompt saved to {prompt_path}")
        print(f"  → estimated ~{est_tokens} tokens "
              f"(from {len(df)} raw messages)")
        print(f"  → compression: {len(df)} msgs → ~{est_tokens} tokens")

    # ---- Console summary ----
    print(f"\n{text_report}")

    return report


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Discord Thai Chat Analysis Pipeline v2')
    parser.add_argument('csv_path', help='Input CSV file path')
    parser.add_argument('-o', '--output', default='./output',
                       help='Output directory (default: ./output)')
    parser.add_argument('-d', '--date', default=None,
                       help='Filter by date YYYY-MM-DD')
    parser.add_argument('--llm-prompt', action='store_true',
                       help='Generate LLM prompt file')
    parser.add_argument('--lang', default='zh', choices=['zh', 'en'],
                       help='LLM prompt language (default: zh)')
    args = parser.parse_args()

    run_pipeline(
        csv_path=args.csv_path,
        target_date=args.date,
        output_dir=args.output,
        generate_prompt=args.llm_prompt,
        lang=args.lang,
    )


if __name__ == '__main__':
    main()
