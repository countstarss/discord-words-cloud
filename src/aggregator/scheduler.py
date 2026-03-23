from __future__ import annotations

import argparse
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..common import load_config
from ..storage import Database, init_db
from .dedup import MessageDeduplicator
from .llm_summary import HierarchicalSummarizer
from .tfidf import TFIDFAnalyzer
from .topic_cluster import TopicClusterer
from .translator import ThaiChineseTranslator


# MARK: - Data Structures
@dataclass
class AggregationResult:
    window_start: datetime
    window_end: datetime
    message_count: int
    keywords: List[Dict[str, object]]
    demand_signals: List[Dict[str, object]]
    summary: str
    topics: List[Dict[str, object]] = field(default_factory=list)
    keyword_cloud: List[Dict[str, object]] = field(default_factory=list)


# MARK: - Demand Signal Extractor (Enhanced V2)
# 规则层负责抓取"想要/问题/抱怨"等高价值意图词。
# V2: 扩展到 30+ 模式词，支持子分类。
class DemandSignalExtractor:
    def __init__(self, patterns: Optional[List[str]] = None):
        if patterns:
            self.patterns = patterns
            self.pattern_to_cn = {p: p for p in patterns}
            self.pattern_to_subtype = {}
        else:
            self._init_default_patterns()

    def _init_default_patterns(self) -> None:
        # 完整的模式词库：{泰语: (中文分类, 子类型)}
        pattern_defs = {
            # 功能诉求类
            "อยาก": ("功能诉求", "新功能需求"),
            "ต้องการ": ("功能诉求", "新功能需求"),
            "ขอ": ("功能诉求", "新功能需求"),
            "เพิ่ม": ("功能诉求", "新功能需求"),
            "อยากได้": ("功能诉求", "新功能需求"),
            "แนะนำ": ("功能诉求", "用户建议"),
            "น่าจะ": ("功能诉求", "用户建议"),
            "ควร": ("功能诉求", "用户建议"),
            # 比较类
            "ดีกว่า": ("竞品比较", "产品对比"),
            "เทียบ": ("竞品比较", "产品对比"),
            "เหมือน": ("竞品比较", "功能类比"),
            "คล้าย": ("竞品比较", "功能类比"),
            # 问题反馈类
            "ปัญหา": ("问题反馈", "一般问题"),
            "ผิด": ("错误异常", "Bug报告"),
            "ไม่ได้": ("无法使用", "功能故障"),
            "ไม่ทำงาน": ("无法使用", "功能故障"),
            "ใช้ไม่ได้": ("无法使用", "功能故障"),
            "ล่ม": ("无法使用", "系统崩溃"),
            "ค้าง": ("性能卡顿", "程序卡死"),
            "bug": ("错误异常", "Bug报告"),
            "error": ("错误异常", "系统错误"),
            "crash": ("无法使用", "系统崩溃"),
            # 求助类
            "ช่วย": ("求助支持", "请求帮助"),
            "ไม่เข้าใจ": ("使用困惑", "理解困难"),
            "ยังไง": ("使用困惑", "不知如何操作"),
            "อย่างไร": ("使用困惑", "不知如何操作"),
            "ทำไม": ("使用困惑", "原因不明"),
            "ไม่รู้": ("使用困惑", "信息缺失"),
            # 情感类
            "ผิดหวัง": ("负面情感", "失望"),
            "เบื่อ": ("负面情感", "厌烦"),
            "โกรธ": ("负面情感", "愤怒"),
            "เสียใจ": ("负面情感", "遗憾"),
            "พอใจ": ("正面情感", "满意"),
            "ชอบ": ("正面情感", "喜欢"),
            "ดีมาก": ("正面情感", "好评"),
            # 价格/支付类
            "แพง": ("价格敏感", "价格偏高"),
            "ถูก": ("价格敏感", "价格优惠"),
            "จ่าย": ("支付相关", "付款"),
            "เติมเงิน": ("支付相关", "充值"),
            "ฝาก": ("支付相关", "存款"),
            "ถอน": ("支付相关", "提现"),
            # 性能类
            "ช้า": ("性能卡顿", "速度慢"),
            "เร็ว": ("性能反馈", "速度快"),
            "หน่วง": ("性能卡顿", "延迟高"),
            # 行动类
            "สมัคร": ("用户行为", "注册"),
            "ยกเลิก": ("用户行为", "取消"),
            "ลบ": ("用户行为", "删除"),
        }

        self.patterns = list(pattern_defs.keys())
        self.pattern_to_cn = {k: v[0] for k, v in pattern_defs.items()}
        self.pattern_to_subtype = {k: v[1] for k, v in pattern_defs.items()}

    def extract(self, texts: List[str], top_n: int = 10) -> List[Dict[str, object]]:
        hits: Counter[str] = Counter()
        samples: Dict[str, str] = {}
        source_patterns: Dict[str, set] = {}
        subtypes: Dict[str, Counter] = {}

        for line in texts:
            normalized = line.lower()
            for pattern in self.patterns:
                if pattern.lower() in normalized:
                    signal_cn = self.pattern_to_cn.get(pattern, pattern)
                    hits[signal_cn] += 1
                    samples.setdefault(signal_cn, line[:200])
                    source_patterns.setdefault(signal_cn, set()).add(pattern)

                    subtype = self.pattern_to_subtype.get(pattern, "")
                    if subtype:
                        subtypes.setdefault(signal_cn, Counter())[subtype] += 1

        result = []
        for signal_cn, count in hits.most_common(top_n):
            entry: Dict[str, object] = {
                "signal": signal_cn,
                "source_patterns": sorted(source_patterns.get(signal_cn, set())),
                "count": int(count),
                "example": samples.get(signal_cn, ""),
            }
            # 添加子类型
            if signal_cn in subtypes:
                entry["sub_signals"] = [
                    {"label": label, "count": int(c)}
                    for label, c in subtypes[signal_cn].most_common(5)
                ]
            result.append(entry)
        return result


# MARK: - Hourly Aggregator
# V2: 集成翻译器、话题聚类、消息去重。
class HourlyAggregator:
    def __init__(self, db: Database, config: Optional[dict] = None):
        self.db = db
        self.config = config or {}
        self.top_n = int(self.config.get("top_n", 50))
        self.min_frequency = int(self.config.get("min_frequency", 2))
        self.window_hours = int(self.config.get("window_hours", 1))

        self.analyzer = TFIDFAnalyzer(max_features=self.top_n, min_frequency=self.min_frequency)
        self.signal_extractor = DemandSignalExtractor(patterns=self.config.get("demand_patterns"))
        self.summarizer = HierarchicalSummarizer(self.config, db=self.db)
        self.deduplicator = MessageDeduplicator()
        self.translator = ThaiChineseTranslator(db=self.db, summarizer=self.summarizer)
        self.clusterer = TopicClusterer()
        self._service_name = "scheduler"

    # MARK: - Window Control
    def _window_bounds(self, current_time: Optional[datetime] = None) -> Tuple[datetime, datetime]:
        now = current_time or datetime.now(timezone.utc)
        window_end = now.replace(minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(hours=self.window_hours)
        return window_start, window_end

    # MARK: - Run Once
    def run_once(self, current_time: Optional[datetime] = None, force_llm: bool = False) -> AggregationResult:
        window_start, window_end = self._window_bounds(current_time)
        self._push_status("running", {"window_start": window_start.isoformat(), "window_end": window_end.isoformat()})
        result = self.run_for_window(window_start=window_start, window_end=window_end, force_llm=force_llm)
        self._push_status(
            "idle",
            {
                "last_window_start": result.window_start.isoformat(),
                "last_window_end": result.window_end.isoformat(),
                "last_message_count": result.message_count,
            },
        )
        return result

    # MARK: - Today Aggregation
    def run_today(
        self,
        timezone_name: str = "Asia/Shanghai",
        current_time: Optional[datetime] = None,
        force_llm: bool = False,
    ) -> AggregationResult:
        now_utc = current_time or datetime.now(timezone.utc)
        try:
            local_tz = ZoneInfo(timezone_name)
        except Exception:
            local_tz = timezone.utc

        now_local = now_utc.astimezone(local_tz)
        day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = day_start_local.astimezone(timezone.utc)
        window_end = now_utc

        self._push_status(
            "running",
            {
                "mode": "today",
                "timezone": timezone_name,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            },
        )
        result = self.run_for_window(window_start=window_start, window_end=window_end, force_llm=force_llm)
        self._push_status(
            "idle",
            {
                "mode": "today",
                "timezone": timezone_name,
                "last_window_start": result.window_start.isoformat(),
                "last_window_end": result.window_end.isoformat(),
                "last_message_count": result.message_count,
            },
        )
        return result

    # MARK: - Window Aggregation (V2 Enhanced)
    def run_for_window(self, window_start: datetime, window_end: datetime, force_llm: bool = False) -> AggregationResult:
        # V2: 支持过滤重复消息和低质量消息
        messages = self.db.get_thai_messages(
            window_start=window_start,
            window_end=window_end,
            exclude_duplicates=True,
            min_quality=0.3,
        )

        texts = [self._message_to_document(m) for m in messages]
        texts = [x for x in texts if x and x.strip()]
        keywords = self.analyzer.analyze(texts)
        demand_signals = self.signal_extractor.extract(texts)

        # V2: 话题聚类
        topics = []
        try:
            topics = self.clusterer.cluster(
                keywords=keywords,
                messages=messages,
                translator=self.translator,
            )
        except Exception:
            pass

        # V2: 翻译关键词 → 中文词云数据
        keyword_cloud = []
        try:
            thai_words = [str(kw["keyword"]) for kw in keywords[:30]]
            translations = self.translator.translate_keywords(thai_words)
            keyword_cloud = [
                {
                    "word_cn": translations.get(str(kw["keyword"]), str(kw["keyword"])),
                    "word_thai": str(kw["keyword"]),
                    "weight": round(float(kw.get("tfidf_score", 0)), 4),
                }
                for kw in keywords[:30]
            ]
        except Exception:
            pass

        summary = self._build_summary(len(messages), keywords, demand_signals)
        llm_result = self.summarizer.summarize_window(
            texts=texts,
            keywords=keywords,
            demand_signals=demand_signals,
            force=force_llm,
        )
        if llm_result:
            summary = str(llm_result.get("summary") or summary)
            if llm_result.get("demand_signals"):
                demand_signals = list(llm_result["demand_signals"])
        demand_signals = self._localize_demand_signals(demand_signals)

        self.db.clear_window_outputs(window_start, window_end)
        self.db.save_hourly_keywords(window_start, window_end, keywords)
        self.db.save_analysis_run(
            window_start=window_start,
            window_end=window_end,
            message_count=len(messages),
            keywords=keywords,
            demand_signals=demand_signals,
            summary=summary,
        )

        return AggregationResult(
            window_start=window_start,
            window_end=window_end,
            message_count=len(messages),
            keywords=keywords,
            demand_signals=demand_signals,
            summary=summary,
            topics=topics,
            keyword_cloud=keyword_cloud,
        )

    def _message_to_document(self, message: object) -> str:
        tokens = getattr(message, "tokens", None)
        if isinstance(tokens, list) and tokens:
            safe_tokens = [str(t).strip() for t in tokens if str(t).strip()]
            if safe_tokens:
                return " ".join(safe_tokens)
        cleaned_text = getattr(message, "cleaned_text", None)
        if isinstance(cleaned_text, str) and cleaned_text.strip():
            return cleaned_text
        content = getattr(message, "content", "")
        return str(content)

    def _localize_demand_signals(self, demand_signals: List[Dict[str, object]]) -> List[Dict[str, object]]:
        cn_map = {
            "feature": "功能诉求",
            "request": "功能诉求",
            "功能": "功能诉求",
            "需求": "功能诉求",
            "help": "求助支持",
            "support": "求助支持",
            "无法": "无法使用",
            "can't": "无法使用",
            "error": "错误异常",
            "bug": "错误异常",
            "problem": "问题反馈",
            "issue": "问题反馈",
            "price": "价格敏感",
            "expensive": "价格敏感",
            "slow": "性能卡顿",
            "performance": "性能卡顿",
        }

        localized = []
        for item in demand_signals:
            signal = str(item.get("signal", "")).strip()
            low = signal.lower()
            mapped = signal
            for key, cn in cn_map.items():
                if key in low:
                    mapped = cn
                    break
            cloned = dict(item)
            cloned["signal"] = mapped
            localized.append(cloned)
        return localized

    # MARK: - Fallback Summary
    def _build_summary(self, count: int, keywords: List[Dict[str, object]], demand_signals: List[Dict[str, object]]) -> str:
        if count == 0:
            return "该小时没有可分析的泰语消息。"

        kw = ", ".join([item["keyword"] for item in keywords[:8]]) or "无显著关键词"
        sig = ", ".join([f"{item['signal']}({item['count']})" for item in demand_signals[:5]]) or "无明显需求信号"
        return f"本小时共分析 {count} 条泰语消息。关键词: {kw}。需求信号: {sig}。"

    # MARK: - Daily Digest Generation (V2)
    def generate_daily_digest(
        self,
        timezone_name: str = "Asia/Bangkok",
        target_date: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """生成每日中文摘要。"""
        now_utc = datetime.now(timezone.utc)
        try:
            local_tz = ZoneInfo(timezone_name)
        except Exception:
            local_tz = timezone.utc

        if target_date:
            day_local = target_date
        else:
            # 默认生成昨天的摘要
            yesterday_local = (now_utc.astimezone(local_tz) - timedelta(days=1))
            day_local = yesterday_local.replace(hour=0, minute=0, second=0, microsecond=0)

        day_start = day_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        window_start = day_start.astimezone(timezone.utc)
        window_end = day_end.astimezone(timezone.utc)

        # 统计数据
        all_messages = self.db.get_thai_messages(window_start=window_start, window_end=window_end)
        total_messages_count = len(all_messages)

        with self.db.session() as db:
            from sqlalchemy import select, func
            from ..storage.models import Message
            total_all = db.scalar(
                select(func.count()).select_from(Message)
                .where(Message.created_at >= window_start)
                .where(Message.created_at < window_end)
                .where(Message.is_deleted.is_(False))
            ) or 0

        active_users = self.db.get_active_user_count(window_start, window_end)
        hourly_volumes = self.db.get_hourly_message_volumes(window_start, window_end)

        # 获取当天的分析结果
        runs = self.db.get_recent_analysis_runs(limit=48)
        day_runs = [
            r for r in runs
            if r.get("window_start") and r["window_start"] >= window_start.isoformat()
            and r.get("window_end") and r["window_end"] <= window_end.isoformat()
        ]

        # 合并关键词
        all_keywords: Counter = Counter()
        all_demand_signals: Counter = Counter()
        demand_examples: Dict[str, str] = {}
        summaries: List[str] = []

        for run in day_runs:
            for kw in run.get("keywords", []) or []:
                keyword = str(kw.get("keyword", ""))
                freq = int(kw.get("frequency", 1))
                all_keywords[keyword] += freq

            for sig in run.get("demand_signals", []) or []:
                signal = str(sig.get("signal", ""))
                count = int(sig.get("count", 1))
                all_demand_signals[signal] += count
                if signal not in demand_examples:
                    demand_examples[signal] = str(sig.get("example", ""))[:200]

            summary = str(run.get("summary", "")).strip()
            if summary:
                summaries.append(summary)

        # 构建词云
        top_kws = all_keywords.most_common(30)
        thai_words = [kw for kw, _ in top_kws]
        translations = self.translator.translate_keywords(thai_words)

        keyword_cloud = [
            {
                "word_cn": translations.get(kw, kw),
                "word_thai": kw,
                "weight": round(freq / max(top_kws[0][1], 1), 4) if top_kws else 0,
            }
            for kw, freq in top_kws
        ]

        # 话题聚类
        keywords_for_cluster = [
            {"keyword": kw, "tfidf_score": freq / max(top_kws[0][1], 1) if top_kws else 0, "frequency": freq}
            for kw, freq in top_kws
        ]
        topics = self.clusterer.cluster(
            keywords=keywords_for_cluster,
            messages=all_messages,
            translator=self.translator,
        )

        # 需求信号汇总
        demand_signal_list = [
            {
                "signal": signal,
                "count": int(count),
                "example": demand_examples.get(signal, ""),
            }
            for signal, count in all_demand_signals.most_common(12)
        ]

        # 生成中文日报
        summary_cn = self._generate_daily_summary_cn(
            total_all=int(total_all),
            thai_count=total_messages_count,
            active_users=active_users,
            keyword_cloud=keyword_cloud,
            demand_signals=demand_signal_list,
            topics=topics,
            hourly_summaries=summaries,
        )

        digest = {
            "digest_date": day_start,
            "timezone": timezone_name,
            "total_messages": int(total_all),
            "thai_messages": total_messages_count,
            "active_users": active_users,
            "summary_cn": summary_cn,
            "top_topics": topics,
            "demand_signals": demand_signal_list,
            "keyword_cloud": keyword_cloud,
            "hourly_volumes": hourly_volumes,
        }

        self.db.save_daily_digest(digest)
        return digest

    def _generate_daily_summary_cn(
        self,
        total_all: int,
        thai_count: int,
        active_users: int,
        keyword_cloud: List[Dict],
        demand_signals: List[Dict],
        topics: List[Dict],
        hourly_summaries: List[str],
    ) -> str:
        """生成中文日报摘要。优先用 LLM，降级用模板。"""
        provider = self.summarizer._resolve_provider()
        if provider:
            prompt = json.dumps(
                {
                    "task": "根据以下数据生成200字中文日报摘要",
                    "data": {
                        "total_messages": total_all,
                        "thai_messages": thai_count,
                        "active_users": active_users,
                        "top_keywords_cn": [k["word_cn"] for k in keyword_cloud[:10]],
                        "demand_signals": [
                            {"signal": s["signal"], "count": s["count"]} for s in demand_signals[:6]
                        ],
                        "topics": [t.get("title_cn", "") for t in topics[:5]],
                        "hourly_summaries": hourly_summaries[:6],
                    },
                    "format": "先写主要讨论主题，再写用户需求，最后给一句产品建议。200字以内。",
                },
                ensure_ascii=False,
            )

            text = self.summarizer._call_llm(
                provider=provider,
                system_prompt="你是资深中文产品分析师。根据泰语社群数据生成中文日报摘要。简洁、有洞察。",
                user_prompt=prompt,
                max_output_tokens=500,
            )
            if text and text.strip():
                return text.strip()

        # 降级模板
        kw_text = "、".join([k["word_cn"] for k in keyword_cloud[:6]]) or "暂无关键词"
        sig_text = "、".join([f"{s['signal']}({s['count']})" for s in demand_signals[:4]]) or "暂无信号"
        topic_text = "、".join([t.get("title_cn", "") for t in topics[:3]]) or "暂无话题"

        return (
            f"今日泰语社群共产生 {total_all} 条消息，其中泰语消息 {thai_count} 条，"
            f"活跃用户 {active_users} 人。"
            f"主要讨论话题：{topic_text}。"
            f"高频关键词：{kw_text}。"
            f"需求信号：{sig_text}。"
        )

    # MARK: - Async Task Runner
    def run_async_analysis(self, task_id: str, mode: str, timezone_name: str = "Asia/Shanghai", force_llm: bool = True) -> None:
        """在后台线程中执行分析任务。"""
        try:
            self.db.update_analysis_task(task_id, status="running", progress=10)

            if mode == "hourly":
                result = self.run_once(force_llm=force_llm)
            elif mode == "today":
                result = self.run_today(timezone_name=timezone_name, force_llm=force_llm)
            elif mode == "daily_digest":
                self.db.update_analysis_task(task_id, progress=50)
                digest = self.generate_daily_digest(timezone_name=timezone_name)
                self.db.update_analysis_task(
                    task_id,
                    status="done",
                    progress=100,
                    result={"digest_date": str(digest.get("digest_date", "")), "summary_cn": digest.get("summary_cn", "")},
                )
                return
            else:
                self.db.update_analysis_task(task_id, status="failed", error=f"Unknown mode: {mode}")
                return

            self.db.update_analysis_task(
                task_id,
                status="done",
                progress=100,
                result={
                    "message_count": result.message_count,
                    "window_start": result.window_start.isoformat(),
                    "window_end": result.window_end.isoformat(),
                    "keywords_count": len(result.keywords),
                    "summary": result.summary[:200],
                },
            )
        except Exception as exc:
            self.db.update_analysis_task(task_id, status="failed", progress=0, error=str(exc)[:500])

    # MARK: - Scheduler Loop
    def run_forever(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            next_run = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
            wait_s = max(int((next_run - now).total_seconds()), 1)
            self._push_status("idle", {"next_run_in_seconds": wait_s})
            time.sleep(wait_s)
            try:
                self.run_once()
                # V2: 每天 UTC 00:05 生成前一天的 digest
                now_after = datetime.now(timezone.utc)
                if now_after.hour == 0:
                    try:
                        tz_name = self.config.get("analysis_timezone", "Asia/Bangkok")
                        self.generate_daily_digest(timezone_name=tz_name)
                    except Exception:
                        pass
            except Exception as exc:
                self._push_status("degraded", {"error": str(exc)})

    # MARK: - Service Heartbeat
    def _push_status(self, state: str, extra: Optional[Dict[str, object]] = None) -> None:
        payload: Dict[str, object] = {"state": state}
        if extra:
            payload.update(extra)
        try:
            self.db.upsert_service_status(self._service_name, payload)
        except Exception:
            pass

    # MARK: - Recompute
    def recompute_window_starts(self, window_starts: List[datetime]) -> List[AggregationResult]:
        results: List[AggregationResult] = []
        for start in sorted(window_starts):
            start_utc = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            end_utc = start_utc + timedelta(hours=self.window_hours)
            results.append(self.run_for_window(window_start=start_utc, window_end=end_utc))
        return results


# MARK: - CLI
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


# MARK: - Main
def main() -> None:
    parser = argparse.ArgumentParser(description="Run hourly aggregation")
    parser.add_argument("--config", default=None)
    parser.add_argument("--loop", action="store_true", help="Run forever at each hour")
    parser.add_argument("--mode", choices=["hourly", "today", "digest"], default="today")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    args = parser.parse_args()

    config = load_config(args.config)
    db = init_db(database_url=_database_url_from_config(config))
    aggregator = HourlyAggregator(db=db, config=config.get("aggregator", {}))

    if args.loop:
        aggregator.run_forever()
    elif args.mode == "digest":
        digest = aggregator.generate_daily_digest(timezone_name=args.timezone)
        print(f"Daily digest generated: {digest.get('summary_cn', '')[:100]}...")
    else:
        if args.mode == "today":
            result = aggregator.run_today(timezone_name=args.timezone)
        else:
            result = aggregator.run_once()
        print(
            f"Aggregated {result.message_count} messages from {result.window_start.isoformat()} to {result.window_end.isoformat()}"
        )


if __name__ == "__main__":
    main()
