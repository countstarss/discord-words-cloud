from __future__ import annotations

import argparse
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..common import load_config
from ..storage import Database, init_db
from .llm_summary import HierarchicalSummarizer
from .tfidf import TFIDFAnalyzer


# MARK: - Data Structures
@dataclass
class AggregationResult:
    window_start: datetime
    window_end: datetime
    message_count: int
    keywords: List[Dict[str, object]]
    demand_signals: List[Dict[str, object]]
    summary: str


# MARK: - Demand Signal Extractor
# 规则层负责抓取“想要/问题/抱怨”等高价值意图词。
class DemandSignalExtractor:
    def __init__(self, patterns: Optional[List[str]] = None):
        self.patterns = patterns or [
            "อยาก", "ต้องการ", "ช่วย", "ไม่ได้", "ปัญหา", "ผิด", "ไม่เข้าใจ", "แพง", "ช้า", "bug", "error"
        ]
        self.pattern_to_cn = {
            "อยาก": "功能诉求",
            "ต้องการ": "功能诉求",
            "ช่วย": "求助支持",
            "ไม่ได้": "无法使用",
            "ปัญหา": "问题反馈",
            "ผิด": "错误异常",
            "ไม่เข้าใจ": "使用困惑",
            "แพง": "价格敏感",
            "ช้า": "性能卡顿",
            "bug": "错误异常",
            "error": "错误异常",
        }

    def extract(self, texts: List[str], top_n: int = 10) -> List[Dict[str, object]]:
        hits: Counter[str] = Counter()
        samples: Dict[str, str] = {}
        source_patterns: Dict[str, set] = {}

        for line in texts:
            normalized = line.lower()
            for pattern in self.patterns:
                if pattern.lower() in normalized:
                    signal_cn = self.pattern_to_cn.get(pattern, pattern)
                    hits[signal_cn] += 1
                    samples.setdefault(signal_cn, line[:200])
                    source_patterns.setdefault(signal_cn, set()).add(pattern)

        result = []
        for signal_cn, count in hits.most_common(top_n):
            result.append({
                "signal": signal_cn,
                "source_patterns": sorted(source_patterns.get(signal_cn, set())),
                "count": int(count),
                "example": samples.get(signal_cn, ""),
            })
        return result


# MARK: - Hourly Aggregator
# 聚合主流程：
# 1) 按小时窗口取泰语消息
# 2) 计算关键词与需求信号
# 3) 可选调用 LLM 做分层摘要
# 4) 覆盖写入该窗口聚合结果
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
    # 按“本地时区当天 00:00 到当前时刻”汇总，适合人工手动触发分析。
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

    # MARK: - Window Aggregation
    def run_for_window(self, window_start: datetime, window_end: datetime, force_llm: bool = False) -> AggregationResult:
        messages = self.db.get_thai_messages(window_start=window_start, window_end=window_end)

        texts = [self._message_to_document(m) for m in messages]
        texts = [x for x in texts if x and x.strip()]
        keywords = self.analyzer.analyze(texts)
        demand_signals = self.signal_extractor.extract(texts)

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
    # 当 LLM 不可用或关闭时，仍需输出可读摘要给看板。
    def _build_summary(self, count: int, keywords: List[Dict[str, object]], demand_signals: List[Dict[str, object]]) -> str:
        if count == 0:
            return "该小时没有可分析的泰语消息。"

        kw = ", ".join([item["keyword"] for item in keywords[:8]]) or "无显著关键词"
        sig = ", ".join([f"{item['signal']}({item['count']})" for item in demand_signals[:5]]) or "无明显需求信号"
        return f"本小时共分析 {count} 条泰语消息。关键词: {kw}。需求信号: {sig}。"

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
    # 用于合规删除后的“受影响小时窗口重算”。
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
    parser.add_argument("--mode", choices=["hourly", "today"], default="today")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    args = parser.parse_args()

    config = load_config(args.config)
    db = init_db(database_url=_database_url_from_config(config))
    aggregator = HourlyAggregator(db=db, config=config.get("aggregator", {}))

    if args.loop:
        aggregator.run_forever()
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
