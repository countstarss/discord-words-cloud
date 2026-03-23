from __future__ import annotations

import argparse
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from ..aggregator.llm_summary import HierarchicalSummarizer
from ..aggregator.scheduler import HourlyAggregator
from ..common import get_secret_cipher, load_config, mask_secret
from ..storage import Database, get_db, init_db

app = FastAPI(title="Discord Thai Collector", version="0.3.0")
_task_executor = ThreadPoolExecutor(max_workers=2)
cipher = get_secret_cipher()


# MARK: - Provider Catalog
# 这里定义前端下拉菜单可选供应商；可按业务继续扩展。
PROVIDER_CATALOG = [
    {
        "provider": "openai",
        "provider_type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1-mini",
    },
    {
        "provider": "deepseek",
        "provider_type": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    {
        "provider": "openrouter",
        "provider_type": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4.1-mini",
    },
    {
        "provider": "xai",
        "provider_type": "openai_compatible",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-2-latest",
    },
    {
        "provider": "anthropic",
        "provider_type": "anthropic",
        "base_url": "",
        "default_model": "claude-3-5-sonnet-latest",
    },
    {
        "provider": "gemini",
        "provider_type": "gemini",
        "base_url": "",
        "default_model": "gemini-1.5-pro",
    },
]


# MARK: - Internal Helpers
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


# 供"合规删除后重算窗口"调用，复用聚合主流程。
def _build_aggregator() -> HourlyAggregator:
    config = load_config()
    db = get_db()
    return HourlyAggregator(db=db, config=config.get("aggregator", {}))


def _aggregate_demand_signals(runs: list[dict]) -> list[dict]:
    counter: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for run in runs:
        for item in run.get("demand_signals", []) or []:
            signal = str(item.get("signal", "")).strip()
            if not signal:
                continue
            count = int(item.get("count", 1) or 1)
            counter[signal] += count
            if signal not in examples:
                examples[signal] = str(item.get("example", ""))[:200]

    result: list[dict] = []
    for signal, count in counter.most_common(12):
        result.append({"signal": signal, "count": int(count), "example": examples.get(signal, "")})
    return result


def _fallback_chinese_insight(keywords: list[dict], demand_signals: list[dict]) -> str:
    kw = [str(x.get("keyword", "")).strip() for x in keywords if str(x.get("keyword", "")).strip()]
    kw = kw[:8]
    ds = [f"{x.get('signal', '')}({x.get('count', 0)})" for x in demand_signals[:6]]
    kw_text = "、".join(kw) if kw else "暂无稳定关键词"
    ds_text = "、".join(ds) if ds else "暂无明显需求信号"
    return (
        f"近24小时讨论主题主要围绕：{kw_text}。"
        f"需求信号集中在：{ds_text}。"
        "建议先按高频信号拆分问题类型，再结合原始消息做二次验证。"
    )


# MARK: - Request Models
class ComplianceDeleteRequest(BaseModel):
    message_ids: list[int] = Field(default_factory=list)
    author_ids: list[int] = Field(default_factory=list)
    hard_delete: bool = False
    recompute_windows: bool = True


class CompliancePurgeRequest(BaseModel):
    retention_days: int = Field(default=14, ge=1, le=3650)
    hard_delete: bool = True


class LLMProviderUpsertRequest(BaseModel):
    provider: str
    provider_type: str = "openai_compatible"
    api_key: str = Field(min_length=6)
    base_url: Optional[str] = None
    model: Optional[str] = None
    enabled: bool = True


class LLMProviderToggleRequest(BaseModel):
    enabled: bool = True


class AnalysisRunRequest(BaseModel):
    mode: str = Field(default="today", pattern="^(today|hourly)$")
    timezone: str = "Asia/Shanghai"
    force_llm: bool = True


class AsyncAnalysisRequest(BaseModel):
    mode: str = Field(default="today", pattern="^(today|hourly|daily_digest)$")
    timezone: str = "Asia/Shanghai"
    force_llm: bool = True


# MARK: - Health & Dashboard APIs
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def status(hours: int = Query(default=24, ge=1, le=168)) -> dict:
    db = get_db()
    return db.get_dashboard_stats(hours=hours)


@app.get("/api/keywords")
def keywords(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[dict]:
    db = get_db()
    return db.get_recent_keywords(hours=hours, limit=limit)


@app.get("/api/runs")
def runs(limit: int = Query(default=24, ge=1, le=200)) -> list[dict]:
    db = get_db()
    return db.get_recent_analysis_runs(limit=limit)


@app.get("/api/services")
def services() -> list[dict]:
    db = get_db()
    return db.get_service_statuses()


@app.get("/api/insights/explain")
def insights_explain(
    hours: int = Query(default=24, ge=1, le=168),
    use_llm: bool = Query(default=False),
) -> dict:
    db = get_db()
    keywords = db.get_recent_keywords(hours=hours, limit=80)
    runs = db.get_recent_analysis_runs(limit=12)
    demand_signals = _aggregate_demand_signals(runs)
    recent_summaries = [str(x.get("summary", "")).strip() for x in runs if str(x.get("summary", "")).strip()]

    llm_text: Optional[str] = None
    if use_llm:
        config = load_config()
        summarizer = HierarchicalSummarizer(config.get("aggregator", {}), db=db)
        llm_text = summarizer.explain_keywords_in_chinese(
            keywords=keywords,
            demand_signals=demand_signals,
            recent_summaries=recent_summaries,
            force=True,
        )

    summary = llm_text or _fallback_chinese_insight(keywords, demand_signals)
    active_provider = db.get_active_llm_provider()
    return {
        "summary": summary,
        "used_llm": bool(llm_text),
        "active_provider": active_provider.get("provider") if active_provider else None,
        "keywords_count": len(keywords),
        "demand_signals_count": len(demand_signals),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/analysis/run")
def run_analysis(req: AnalysisRunRequest) -> dict:
    aggregator = _build_aggregator()
    if req.mode == "hourly":
        result = aggregator.run_once(force_llm=req.force_llm)
    else:
        result = aggregator.run_today(timezone_name=req.timezone, force_llm=req.force_llm)
    return {
        "ok": True,
        "mode": req.mode,
        "timezone": req.timezone,
        "message_count": result.message_count,
        "window_start": result.window_start.isoformat(),
        "window_end": result.window_end.isoformat(),
        "summary": result.summary,
        "keywords_count": len(result.keywords),
        "demand_signals_count": len(result.demand_signals),
    }


# MARK: - LLM Provider APIs
@app.get("/api/llm/catalog")
def llm_catalog() -> list[dict]:
    return PROVIDER_CATALOG


@app.get("/api/llm/providers")
def llm_providers() -> list[dict]:
    db = get_db()
    rows = db.list_llm_providers()
    result = []
    for row in rows:
        decrypted = cipher.decrypt(str(row.get("api_key_encrypted") or ""))
        result.append(
            {
                "provider": row["provider"],
                "provider_type": row["provider_type"],
                "base_url": row.get("base_url"),
                "model": row.get("model"),
                "enabled": row.get("enabled", False),
                "updated_at": row.get("updated_at"),
                "api_key_masked": mask_secret(decrypted),
                "has_key": bool(decrypted),
            }
        )
    return result


@app.post("/api/llm/providers")
def llm_upsert_provider(req: LLMProviderUpsertRequest) -> dict:
    db = get_db()
    encrypted = cipher.encrypt(req.api_key.strip())
    db.upsert_llm_provider(
        provider=req.provider,
        provider_type=req.provider_type,
        api_key_encrypted=encrypted,
        base_url=(req.base_url or "").strip() or None,
        model=(req.model or "").strip() or None,
        enabled=req.enabled,
    )
    return {"ok": True}


@app.post("/api/llm/providers/{provider}/enable")
def llm_set_provider_enabled(provider: str, req: LLMProviderToggleRequest) -> dict:
    db = get_db()
    ok = db.set_llm_provider_enabled(provider=provider, enabled=req.enabled)
    return {"ok": ok}


@app.delete("/api/llm/providers/{provider}")
def llm_delete_provider(provider: str) -> dict:
    db = get_db()
    ok = db.delete_llm_provider(provider=provider)
    return {"ok": ok}


# MARK: - Compliance APIs
@app.post("/api/compliance/delete")
def compliance_delete(req: ComplianceDeleteRequest) -> dict:
    db = get_db()
    result = db.compliance_delete(
        message_ids=req.message_ids,
        author_ids=req.author_ids,
        hard_delete=req.hard_delete,
    )

    recomputed = []
    if req.recompute_windows and result["affected_window_starts"]:
        aggregator = _build_aggregator()
        starts = []
        for item in result["affected_window_starts"]:
            dt = datetime.fromisoformat(item)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            starts.append(dt)
        recomputed = [
            {
                "window_start": r.window_start.isoformat(),
                "window_end": r.window_end.isoformat(),
                "message_count": r.message_count,
            }
            for r in aggregator.recompute_window_starts(starts)
        ]

    return {
        "deleted_or_marked": result["matched"],
        "affected_window_starts": result["affected_window_starts"],
        "recomputed_windows": recomputed,
    }


@app.post("/api/compliance/purge")
def compliance_purge(req: CompliancePurgeRequest) -> dict:
    db = get_db()
    return db.purge_raw_messages(retention_days=req.retention_days, hard_delete=req.hard_delete)


# MARK: - V2 API Endpoints
# 面向前端的结构化中文 JSON，一次请求获取全部数据。

@app.get("/api/v2/dashboard")
def v2_dashboard(hours: int = Query(default=24, ge=1, le=168)) -> dict:
    """一次请求获取前端所需的全部数据。"""
    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    stats = db.get_dashboard_stats(hours=hours)
    keywords = db.get_recent_keywords_with_translations(hours=hours, limit=60)
    runs = db.get_recent_analysis_runs(limit=24)
    demand_signals = _aggregate_demand_signals(runs)
    hourly_volumes = db.get_hourly_message_volumes(
        window_start=cutoff,
        window_end=datetime.now(timezone.utc),
    )

    # 构建词云数据
    keyword_cloud = []
    seen = set()
    for kw in keywords:
        cn = kw.get("keyword_cn", kw["keyword"])
        if cn in seen:
            continue
        seen.add(cn)
        keyword_cloud.append({
            "word_cn": cn,
            "word_thai": kw["keyword"],
            "weight": round(kw["tfidf_score"], 4),
        })
        if len(keyword_cloud) >= 30:
            break

    # 从最近的 runs 提取话题（如果有）
    top_topics = []
    for run in runs[:6]:
        for kw in run.get("keywords", [])[:3]:
            keyword = str(kw.get("keyword", ""))
            cn = next((k.get("keyword_cn", keyword) for k in keywords if k["keyword"] == keyword), keyword)
            if cn not in [t.get("title_cn") for t in top_topics]:
                top_topics.append({
                    "title_cn": cn,
                    "keywords_cn": [cn],
                    "message_count": run.get("message_count", 0),
                    "heat_score": round(float(kw.get("tfidf_score", 0)) * 2, 2),
                })
            if len(top_topics) >= 5:
                break
        if len(top_topics) >= 5:
            break

    return {
        "metrics": {
            "total_24h": stats["total_messages"],
            "thai_24h": stats["thai_messages"],
            "thai_ratio": f"{stats['thai_ratio']:.1f}%",
            "active_users_24h": stats.get("active_users", 0),
            "active_provider": stats.get("active_llm_provider"),
        },
        "hourly_volumes": hourly_volumes,
        "top_topics": top_topics,
        "demand_signals": [
            {
                "signal_cn": sig["signal"],
                "count": sig["count"],
                "example": sig.get("example", ""),
            }
            for sig in demand_signals
        ],
        "keyword_cloud": keyword_cloud,
        "services": stats.get("services", []),
    }


@app.get("/api/v2/daily-digest")
def v2_daily_digest(
    date: Optional[str] = Query(default=None, description="Date in YYYY-MM-DD format"),
    timezone_name: str = Query(default="Asia/Bangkok", alias="timezone"),
) -> dict:
    """获取每日中文摘要。"""
    db = get_db()

    if date:
        try:
            target = datetime.strptime(date, "%Y-%m-%d")
            try:
                local_tz = ZoneInfo(timezone_name)
            except Exception:
                local_tz = timezone.utc
            target = target.replace(tzinfo=local_tz)
        except ValueError:
            return {"error": f"Invalid date format: {date}, expected YYYY-MM-DD"}
    else:
        try:
            local_tz = ZoneInfo(timezone_name)
        except Exception:
            local_tz = timezone.utc
        target = (datetime.now(timezone.utc).astimezone(local_tz) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    digest = db.get_daily_digest(target)

    if digest:
        return {
            "date": date or target.strftime("%Y-%m-%d"),
            "summary_cn": digest["summary_cn"],
            "metrics": {
                "total_messages": digest["total_messages"],
                "thai_messages": digest["thai_messages"],
                "active_users": digest["active_users"],
            },
            "top_topics": digest["top_topics"],
            "demand_signals": digest["demand_signals"],
            "keyword_cloud": digest["keyword_cloud"],
            "hourly_volumes": digest["hourly_volumes"],
        }

    # 如果没有缓存的 digest，实时生成
    aggregator = _build_aggregator()
    try:
        digest_data = aggregator.generate_daily_digest(
            timezone_name=timezone_name,
            target_date=target,
        )
        return {
            "date": date or target.strftime("%Y-%m-%d"),
            "summary_cn": digest_data.get("summary_cn", ""),
            "metrics": {
                "total_messages": digest_data.get("total_messages", 0),
                "thai_messages": digest_data.get("thai_messages", 0),
                "active_users": digest_data.get("active_users", 0),
            },
            "top_topics": digest_data.get("top_topics", []),
            "demand_signals": digest_data.get("demand_signals", []),
            "keyword_cloud": digest_data.get("keyword_cloud", []),
            "hourly_volumes": digest_data.get("hourly_volumes", []),
        }
    except Exception as exc:
        return {"error": str(exc), "date": date or target.strftime("%Y-%m-%d")}


@app.get("/api/v2/trends")
def v2_trends(
    days: int = Query(default=7, ge=1, le=30),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict:
    """获取过去 N 天的关键词趋势。"""
    db = get_db()
    keyword_trends = db.get_keyword_trends(days=days, limit=limit)

    # 按信号维度也做趋势
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    runs = db.get_recent_analysis_runs(limit=days * 24)

    from collections import defaultdict
    signal_daily: dict = defaultdict(lambda: defaultdict(int))
    for run in runs:
        ws = run.get("window_start", "")
        if ws < cutoff.isoformat():
            continue
        day_str = ws[:10]
        for sig in run.get("demand_signals", []) or []:
            signal = str(sig.get("signal", ""))
            count = int(sig.get("count", 1))
            signal_daily[signal][day_str] += count

    signal_trends = []
    for signal, daily in sorted(signal_daily.items(), key=lambda x: sum(x[1].values()), reverse=True)[:10]:
        dates = sorted(daily.keys())
        counts = [daily[d] for d in dates]
        total = sum(counts)

        if len(counts) >= 2:
            recent = sum(counts[-2:])
            earlier = sum(counts[:2]) if len(counts) >= 4 else counts[0]
            if earlier > 0 and recent / max(earlier, 1) > 1.5:
                status = "上升"
            elif earlier > 0 and recent / max(earlier, 1) < 0.5:
                status = "下降"
            else:
                status = "稳定"
        else:
            status = "数据不足"

        signal_trends.append({
            "signal_cn": signal,
            "dates": [d[5:] for d in dates],
            "daily_counts": counts,
            "total": total,
            "status": status,
        })

    return {
        "days": days,
        "topic_trends": [
            {
                "topic_cn": t["keyword_cn"],
                "topic_thai": t["keyword_thai"],
                "dates": t["dates"],
                "daily_counts": t["daily_counts"],
                "total": t["total"],
                "status": t["status"],
            }
            for t in keyword_trends
        ],
        "signal_trends": signal_trends,
    }


@app.get("/api/v2/translations")
def v2_translations(limit: int = Query(default=500, ge=1, le=5000)) -> list[dict]:
    """获取已缓存的关键词翻译。"""
    db = get_db()
    return db.get_all_translations(limit=limit)


# MARK: - V2 Async Analysis
@app.post("/api/v2/analysis/run")
def v2_run_analysis_async(req: AsyncAnalysisRequest) -> dict:
    """异步触发分析任务，立即返回 task_id。"""
    db = get_db()
    task_id = str(uuid.uuid4())
    db.create_analysis_task(task_id=task_id, mode=req.mode)

    aggregator = _build_aggregator()
    _task_executor.submit(
        aggregator.run_async_analysis,
        task_id=task_id,
        mode=req.mode,
        timezone_name=req.timezone,
        force_llm=req.force_llm,
    )

    return {"task_id": task_id, "status": "pending"}


@app.get("/api/v2/analysis/tasks/{task_id}")
def v2_get_task_status(task_id: str) -> dict:
    """查询异步分析任务进度。"""
    db = get_db()
    task = db.get_analysis_task(task_id)
    if task is None:
        return {"error": "Task not found", "task_id": task_id}
    return task


# MARK: - Dashboard Page
# 页面采用"单文件模板"方式，便于快速内嵌发布与迁移。
@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return """
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Pulseboard · Discord Thai Collector</title>
    <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
    <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
    <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Sora:wght@400;500;600;700&display=swap\" rel=\"stylesheet\">
    <style>
      :root {
        --bg: #f5f7fb;
        --panel: #ffffff;
        --text: #0f172a;
        --muted: #64748b;
        --line: #e2e8f0;
        --accent: #2563eb;
        --accent-2: #0891b2;
        --danger: #b91c1c;
        --radius: 14px;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Sora", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at 20% 0%, rgba(37,99,235,.10), transparent 42%),
          radial-gradient(circle at 100% 0%, rgba(8,145,178,.10), transparent 44%),
          var(--bg);
      }
      .shell {
        max-width: 1320px;
        margin: 0 auto;
        padding: 26px 20px 40px;
      }
      .topbar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 16px;
      }
      .brand {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .brand-dot {
        width: 11px;
        height: 11px;
        border-radius: 999px;
        background: linear-gradient(135deg, var(--accent), var(--accent-2));
        box-shadow: 0 0 0 5px rgba(37,99,235,.14);
      }
      .title {
        font-size: 24px;
        font-weight: 700;
        letter-spacing: -0.02em;
      }
      .subtitle {
        margin-top: 4px;
        color: var(--muted);
        font-size: 12px;
      }
      .pill {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 8px 12px;
        background: var(--panel);
        color: var(--muted);
        font-size: 12px;
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: #22c55e;
      }
      .metrics {
        display:grid;
        grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: 12px;
        margin: 14px 0 20px;
      }
      .metric {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: var(--radius);
        padding: 14px;
        box-shadow: 0 8px 24px rgba(15,23,42,.05);
      }
      .metric .label {
        color: var(--muted);
        font-size: 12px;
      }
      .metric .value {
        margin-top: 8px;
        font-size: 27px;
        font-weight: 650;
        letter-spacing: -0.02em;
      }
      .layout {
        display: grid;
        grid-template-columns: 1fr;
        gap: 14px;
      }
      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: var(--radius);
        box-shadow: 0 8px 24px rgba(15,23,42,.05);
        overflow: hidden;
      }
      .panel-head {
        padding: 14px 16px;
        border-bottom: 1px solid var(--line);
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
      }
      .panel-title {
        font-size: 14px;
        font-weight: 650;
        letter-spacing: .01em;
      }
      .panel-body {
        padding: 12px;
        max-height: 520px;
        overflow: auto;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th, td {
        border-bottom: 1px solid var(--line);
        text-align: left;
        padding: 10px 11px;
        font-size: 12.5px;
        vertical-align: top;
      }
      th {
        color: var(--muted);
        font-weight: 600;
        background: #f8fafc;
      }
      tr:last-child td {
        border-bottom: none;
      }
      .mono, code {
        font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12px;
      }
      code {
        background: #eef2ff;
        color: #1e40af;
        border-radius: 7px;
        padding: 2px 6px;
      }
      .form-grid {
        display:grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 10px;
      }
      .form-item {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .form-item label {
        color: var(--muted);
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: .03em;
      }
      input, select, button {
        font-family: "Sora", sans-serif;
        font-size: 12.5px;
        border-radius: 10px;
      }
      input, select {
        border: 1px solid #cbd5e1;
        padding: 10px;
        background: #fff;
      }
      .actions {
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
      }
      button {
        border: none;
        background: linear-gradient(135deg, var(--accent), #1d4ed8);
        color: #fff;
        padding: 10px 13px;
        cursor: pointer;
        font-weight: 600;
      }
      button.secondary {
        background: #334155;
      }
      button.danger {
        background: var(--danger);
      }
      .hint {
        font-size: 11px;
        color: var(--muted);
      }
      .insight-text {
        font-size: 14px;
        line-height: 1.7;
        color: #0f172a;
        white-space: pre-wrap;
      }
      .insight-meta {
        margin-top: 10px;
        font-size: 12px;
        color: var(--muted);
      }
      @media (min-width: 1080px) {
        .layout {
          grid-template-columns: 1.25fr .95fr;
        }
      }
    </style>
  </head>
  <body>
    <div class=\"shell\">
      <header class=\"topbar\">
        <div class=\"brand\">
          <span class=\"brand-dot\"></span>
          <div>
            <div class=\"title\">Pulseboard</div>
            <div class=\"subtitle\">Discord Thai Collector · Realtime Intelligence Surface</div>
          </div>
        </div>
        <div class=\"pill\"><span class=\"dot\"></span><span>Auto refresh every 30 seconds</span></div>
      </header>

      <section class=\"metrics\" id=\"stats\"></section>

      <section class=\"layout\">
        <div class=\"panel\">
          <div class=\"panel-head\">
            <div class=\"panel-title\">Top Keywords · Last 24h</div>
            <div class=\"hint\">TF-IDF + frequency</div>
          </div>
          <div class=\"panel-body\">
            <table>
              <thead><tr><th>Window Start</th><th>Keyword</th><th>TF-IDF</th><th>Freq</th></tr></thead>
              <tbody id=\"kw-body\"></tbody>
            </table>
          </div>
        </div>

        <div class=\"panel\">
          <div class=\"panel-head\">
            <div class=\"panel-title\">Service Health</div>
            <div class=\"hint\">collector / scheduler / web</div>
          </div>
          <div class=\"panel-body\">
            <table>
              <thead><tr><th>Service</th><th>State</th><th>Updated</th><th>Payload</th></tr></thead>
              <tbody id=\"svc-body\"></tbody>
            </table>
          </div>
        </div>

        <div class=\"panel\">
          <div class=\"panel-head\">
            <div class=\"panel-title\">Hourly Insights</div>
            <div class=\"actions\">
              <button class=\"secondary\" onclick=\"runAnalysisNow('today')\">Run Today</button>
              <button class=\"secondary\" onclick=\"runAnalysisNow('hourly')\">Run Hourly</button>
            </div>
          </div>
          <div class=\"panel-body\">
            <table>
              <thead><tr><th>Window</th><th>Messages</th><th>Summary</th></tr></thead>
              <tbody id=\"runs-body\"></tbody>
            </table>
          </div>
        </div>

        <div class=\"panel\">
          <div class=\"panel-head\">
            <div class=\"panel-title\">中文洞察摘要</div>
            <div class=\"actions\">
              <button class=\"secondary\" onclick=\"refreshInsight(false)\">Refresh</button>
              <button onclick=\"refreshInsight(true)\">Summarize With LLM</button>
            </div>
          </div>
          <div class=\"panel-body\">
            <div id=\"insight-summary\" class=\"insight-text\">暂无洞察，请先收集消息并执行分析。</div>
            <div id=\"insight-meta\" class=\"insight-meta\"></div>
          </div>
        </div>

        <div class=\"panel\">
          <div class=\"panel-head\">
            <div class=\"panel-title\">LLM Provider Console</div>
            <div class=\"hint\">multi-vendor key management</div>
          </div>
          <div class=\"panel-body\">
            <div class=\"form-grid\">
              <div class=\"form-item\">
                <label>Provider</label>
                <select id=\"provider-select\"></select>
              </div>
              <div class=\"form-item\">
                <label>Type</label>
                <select id=\"provider-type\">
                  <option value=\"openai_compatible\">openai_compatible</option>
                  <option value=\"anthropic\">anthropic</option>
                  <option value=\"gemini\">gemini</option>
                </select>
              </div>
              <div class=\"form-item\">
                <label>Base URL</label>
                <input id=\"provider-base-url\" placeholder=\"optional\" />
              </div>
              <div class=\"form-item\">
                <label>Model</label>
                <input id=\"provider-model\" placeholder=\"model id\" />
              </div>
              <div class=\"form-item\" style=\"grid-column: span 2;\">
                <label>API Key</label>
                <input id=\"provider-key\" type=\"password\" placeholder=\"paste provider key\" />
              </div>
              <div class=\"form-item\">
                <label>Enabled</label>
                <div class=\"actions\"><label><input type=\"checkbox\" id=\"provider-enabled\" checked/> active</label></div>
              </div>
              <div class=\"form-item\">
                <label>Action</label>
                <div class=\"actions\"><button onclick=\"saveProvider()\">Save Provider</button></div>
              </div>
            </div>
            <div style=\"height:10px\"></div>
            <table>
              <thead><tr><th>Provider</th><th>Type</th><th>Model</th><th>Base URL</th><th>Key</th><th>Enabled</th><th>Updated</th><th>Actions</th></tr></thead>
              <tbody id=\"llm-body\"></tbody>
            </table>
          </div>
        </div>
      </section>
    </div>

    <script>
      let providerCatalog = [];

      async function fetchJson(url){
        const res = await fetch(url);
        return await res.json();
      }

      async function postJson(url, body, method='POST'){
        const res = await fetch(url, {
          method,
          headers: {'Content-Type':'application/json'},
          body: body ? JSON.stringify(body) : undefined
        });
        return await res.json();
      }

      function metricCard(title, value){
        return `<div class=\"metric\"><div class=\"label\">${title}</div><div class=\"value\">${value}</div></div>`;
      }

      function onProviderChanged(){
        const selected = document.getElementById('provider-select').value;
        const found = providerCatalog.find(x => x.provider === selected);
        if(!found) return;
        document.getElementById('provider-type').value = found.provider_type;
        document.getElementById('provider-base-url').value = found.base_url || '';
        document.getElementById('provider-model').value = found.default_model || '';
      }

      async function loadProviderCatalog(){
        providerCatalog = await fetchJson('/api/llm/catalog');
        const select = document.getElementById('provider-select');
        select.innerHTML = providerCatalog.map(x => `<option value=\"${x.provider}\">${x.provider}</option>`).join('');
        select.onchange = onProviderChanged;
        onProviderChanged();
      }

      async function saveProvider(){
        const payload = {
          provider: document.getElementById('provider-select').value,
          provider_type: document.getElementById('provider-type').value,
          base_url: document.getElementById('provider-base-url').value,
          model: document.getElementById('provider-model').value,
          api_key: document.getElementById('provider-key').value,
          enabled: document.getElementById('provider-enabled').checked
        };
        if(!payload.api_key){
          alert('API key is required');
          return;
        }
        await postJson('/api/llm/providers', payload, 'POST');
        document.getElementById('provider-key').value = '';
        await refreshLLM();
        await refresh();
      }

      async function enableProvider(provider, enabled){
        await postJson(`/api/llm/providers/${provider}/enable`, {enabled}, 'POST');
        await refreshLLM();
        await refresh();
      }

      async function deleteProvider(provider){
        if(!confirm(`Delete provider ${provider}?`)) return;
        await postJson(`/api/llm/providers/${provider}`, null, 'DELETE');
        await refreshLLM();
        await refresh();
      }

      async function refreshLLM(){
        const providers = await fetchJson('/api/llm/providers');
        document.getElementById('llm-body').innerHTML = providers.map(item => `
          <tr>
            <td><code>${item.provider}</code></td>
            <td>${item.provider_type}</td>
            <td>${item.model || ''}</td>
            <td>${item.base_url || ''}</td>
            <td><code>${item.api_key_masked || ''}</code></td>
            <td>${item.enabled ? 'yes' : 'no'}</td>
            <td>${item.updated_at ? new Date(item.updated_at).toLocaleString() : ''}</td>
            <td>
              <button class=\"secondary\" onclick=\"enableProvider('${item.provider}', true)\">Enable</button>
              <button class=\"secondary\" onclick=\"enableProvider('${item.provider}', false)\">Disable</button>
              <button class=\"danger\" onclick=\"deleteProvider('${item.provider}')\">Delete</button>
            </td>
          </tr>
        `).join('');
      }

      async function refresh(){
        const status = await fetchJson('/api/status?hours=24');
        const keywords = await fetchJson('/api/keywords?hours=24&limit=30');
        const runs = await fetchJson('/api/runs?limit=10');
        const services = await fetchJson('/api/services');

        document.getElementById('stats').innerHTML = [
          metricCard('24h 总消息', status.total_messages),
          metricCard('24h 泰语消息', status.thai_messages),
          metricCard('泰语占比', status.thai_ratio.toFixed(2) + '%'),
          metricCard('LLM Provider', status.active_llm_provider || 'N/A'),
          metricCard('最近消息', status.last_message_at ? new Date(status.last_message_at).toLocaleString() : 'N/A'),
          metricCard('最近分析窗口', status.last_analysis_window_end ? new Date(status.last_analysis_window_end).toLocaleString() : 'N/A')
        ].join('');

        document.getElementById('kw-body').innerHTML = keywords.slice(0,30).map(item => `
          <tr>
            <td>${new Date(item.window_start).toLocaleString()}</td>
            <td><code>${item.keyword}</code></td>
            <td>${item.tfidf_score.toFixed(4)}</td>
            <td>${item.frequency}</td>
          </tr>
        `).join('');

        document.getElementById('runs-body').innerHTML = runs.map(item => `
          <tr>
            <td>${new Date(item.window_start).toLocaleString()} ~ ${new Date(item.window_end).toLocaleTimeString()}</td>
            <td>${item.message_count}</td>
            <td>${item.summary || ''}</td>
          </tr>
        `).join('');

        document.getElementById('svc-body').innerHTML = services.map(item => `
          <tr>
            <td><code>${item.service_name}</code></td>
            <td>${item.status.state || 'unknown'}</td>
            <td>${item.updated_at ? new Date(item.updated_at).toLocaleString() : ''}</td>
            <td class=\"mono\">${JSON.stringify(item.status)}</td>
          </tr>
        `).join('');
      }

      async function refreshInsight(useLLM=false){
        const q = useLLM ? 'true' : 'false';
        const data = await fetchJson(`/api/insights/explain?hours=24&use_llm=${q}`);
        document.getElementById('insight-summary').textContent = data.summary || '暂无洞察';
        const mode = data.used_llm ? `LLM(${data.active_provider || 'unknown'})` : 'rule-based';
        const ts = data.updated_at ? new Date(data.updated_at).toLocaleString() : '';
        document.getElementById('insight-meta').textContent =
          `来源: ${mode} · 关键词:${data.keywords_count} · 需求信号:${data.demand_signals_count} · 更新时间:${ts}`;
      }

      async function runAnalysisNow(mode){
        const body = {
          mode: mode || 'today',
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
          force_llm: true
        };
        const res = await postJson('/api/analysis/run', body, 'POST');
        if(!res || !res.ok){
          alert('Analysis run failed');
          return;
        }
        await refresh();
        await refreshLLM();
        await refreshInsight(true);
        alert(`Analysis done: ${res.message_count} messages`);
      }

      async function bootstrap(){
        await loadProviderCatalog();
        await refreshLLM();
        await refresh();
        await refreshInsight(false);
      }

      bootstrap();
      setInterval(async () => {
        await refresh();
        await refreshLLM();
        await refreshInsight(false);
      }, 30000);
    </script>
  </body>
</html>
"""


# MARK: - Web Runtime
def run_web(config_path: Optional[str] = None) -> None:
    config = load_config(config_path)
    database_url = _database_url_from_config(config)
    db = init_db(database_url=database_url)
    db.upsert_service_status("web", {"state": "running"})

    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "0.0.0.0")
    port = int(web_cfg.get("port", 8080))

    uvicorn.run("src.api.app:app", host=host, port=port, reload=False)


# MARK: - Main
def main() -> None:
    parser = argparse.ArgumentParser(description="Run dashboard web server")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    run_web(config_path=args.config)


if __name__ == "__main__":
    main()
