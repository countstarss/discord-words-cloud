from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional

from sqlalchemy import create_engine, delete, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AnalysisRun,
    AnalysisTask,
    Base,
    DailyDigest,
    HourlyKeyword,
    KeywordTranslation,
    LLMProviderCredential,
    Message,
    ServiceHeartbeat,
)


# MARK: - Config
# 从环境变量构建连接串，支持本地直连与容器化部署。
def _database_url_from_env() -> str:
    raw_url = os.getenv("DATABASE_URL")
    if raw_url:
        return raw_url

    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "discord_thai")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


# MARK: - Database Gateway
# 统一封装所有数据库读写，避免业务层直接拼 SQL。
class Database:
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or _database_url_from_env()
        self.engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
        self._session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )

    # MARK: - Session
    def init_tables(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self._session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # MARK: - Message CRUD
    # PostgreSQL 走原生 upsert；SQLite 走兼容路径。
    def upsert_message(self, payload: Dict[str, Any]) -> bool:
        with self.session() as db:
            existing = db.get(Message, payload["message_id"])
            if existing is None:
                db.add(Message(**payload))
                return True

            for key, value in payload.items():
                if key == "message_id":
                    continue
                setattr(existing, key, value)
            return False

    def mark_deleted(self, message_id: int, deleted_at: Optional[datetime] = None) -> None:
        deleted_at = deleted_at or datetime.now(timezone.utc)
        with self.session() as db:
            msg = db.get(Message, message_id)
            if msg is None:
                return
            msg.is_deleted = True
            msg.event_type = "delete"
            msg.deleted_at = deleted_at
            msg.updated_at = deleted_at

    # MARK: - Message Query
    def get_thai_messages(
        self,
        window_start: datetime,
        window_end: datetime,
        exclude_duplicates: bool = False,
        min_quality: float = 0.0,
    ) -> List[Message]:
        with self.session() as db:
            stmt = (
                select(Message)
                .where(Message.is_thai.is_(True))
                .where(Message.is_deleted.is_(False))
                .where(Message.created_at >= window_start)
                .where(Message.created_at < window_end)
            )
            if exclude_duplicates:
                stmt = stmt.where(Message.is_duplicate.is_(False))
            if min_quality > 0:
                stmt = stmt.where(Message.quality_score >= min_quality)
            stmt = stmt.order_by(Message.created_at.asc())
            return list(db.scalars(stmt))

    # MARK: - Aggregation Persistence
    def save_hourly_keywords(
        self,
        window_start: datetime,
        window_end: datetime,
        keywords: Iterable[Dict[str, Any]],
    ) -> None:
        rows = list(keywords)
        if not rows:
            return

        if self.engine.dialect.name == "postgresql":
            with self.session() as db:
                for item in rows:
                    stmt = pg_insert(HourlyKeyword).values(
                        window_start=window_start,
                        window_end=window_end,
                        keyword=item["keyword"],
                        tfidf_score=float(item["tfidf_score"]),
                        frequency=int(item["frequency"]),
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[HourlyKeyword.window_start, HourlyKeyword.keyword],
                        set_={
                            "window_end": stmt.excluded.window_end,
                            "tfidf_score": stmt.excluded.tfidf_score,
                            "frequency": stmt.excluded.frequency,
                            "created_at": func.now(),
                        },
                    )
                    db.execute(stmt)
            return

        with self.session() as db:
            for item in rows:
                existing = db.scalar(
                    select(HourlyKeyword).where(
                        HourlyKeyword.window_start == window_start,
                        HourlyKeyword.keyword == item["keyword"],
                    )
                )
                if existing is None:
                    db.add(
                        HourlyKeyword(
                            window_start=window_start,
                            window_end=window_end,
                            keyword=item["keyword"],
                            tfidf_score=float(item["tfidf_score"]),
                            frequency=int(item["frequency"]),
                        )
                    )
                    continue
                existing.window_end = window_end
                existing.tfidf_score = float(item["tfidf_score"])
                existing.frequency = int(item["frequency"])

    def save_analysis_run(
        self,
        window_start: datetime,
        window_end: datetime,
        message_count: int,
        keywords: List[Dict[str, Any]],
        demand_signals: List[Dict[str, Any]],
        summary: str,
    ) -> None:
        with self.session() as db:
            db.add(
                AnalysisRun(
                    window_start=window_start,
                    window_end=window_end,
                    message_count=message_count,
                    keywords=keywords,
                    demand_signals=demand_signals,
                    summary=summary,
                )
            )

    # MARK: - Window Rebuild
    # 重算某窗口前先删旧结果，避免重复累计。
    def clear_window_outputs(self, window_start: datetime, window_end: datetime) -> None:
        with self.session() as db:
            db.execute(delete(HourlyKeyword).where(HourlyKeyword.window_start == window_start))
            db.execute(
                delete(AnalysisRun).where(
                    AnalysisRun.window_start == window_start,
                    AnalysisRun.window_end == window_end,
                )
            )

    # MARK: - Keyword Translation Cache
    def get_keyword_translations(self, keywords_thai: List[str]) -> Dict[str, str]:
        """查询已缓存的翻译，返回 {thai: cn}。"""
        if not keywords_thai:
            return {}

        with self.session() as db:
            stmt = select(KeywordTranslation).where(
                KeywordTranslation.keyword_thai.in_(keywords_thai)
            )
            rows = list(db.scalars(stmt))

        return {row.keyword_thai: row.keyword_cn for row in rows}

    def upsert_keyword_translation(
        self,
        keyword_thai: str,
        keyword_cn: str,
        keyword_en: Optional[str] = None,
        category: Optional[str] = None,
    ) -> None:
        """写入或更新关键词翻译缓存。"""
        if self.engine.dialect.name == "postgresql":
            with self.session() as db:
                stmt = pg_insert(KeywordTranslation).values(
                    keyword_thai=keyword_thai,
                    keyword_cn=keyword_cn,
                    keyword_en=keyword_en,
                    category=category,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[KeywordTranslation.keyword_thai],
                    set_={
                        "keyword_cn": stmt.excluded.keyword_cn,
                        "keyword_en": stmt.excluded.keyword_en,
                        "category": stmt.excluded.category,
                    },
                )
                db.execute(stmt)
            return

        with self.session() as db:
            existing = db.get(KeywordTranslation, keyword_thai)
            if existing is None:
                db.add(
                    KeywordTranslation(
                        keyword_thai=keyword_thai,
                        keyword_cn=keyword_cn,
                        keyword_en=keyword_en,
                        category=category,
                    )
                )
            else:
                existing.keyword_cn = keyword_cn
                if keyword_en is not None:
                    existing.keyword_en = keyword_en
                if category is not None:
                    existing.category = category

    def get_all_translations(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """获取所有翻译缓存。"""
        with self.session() as db:
            stmt = select(KeywordTranslation).limit(limit)
            rows = list(db.scalars(stmt))

        return [
            {
                "keyword_thai": row.keyword_thai,
                "keyword_cn": row.keyword_cn,
                "keyword_en": row.keyword_en,
                "category": row.category,
            }
            for row in rows
        ]

    # MARK: - Daily Digest
    def save_daily_digest(self, digest: Dict[str, Any]) -> None:
        """保存每日摘要。如果该日期已存在则更新。"""
        digest_date = digest["digest_date"]

        with self.session() as db:
            existing = db.scalar(
                select(DailyDigest).where(DailyDigest.digest_date == digest_date)
            )
            if existing is None:
                db.add(DailyDigest(**digest))
            else:
                for key, value in digest.items():
                    if key != "id":
                        setattr(existing, key, value)

    def get_daily_digest(self, digest_date: datetime) -> Optional[Dict[str, Any]]:
        """获取指定日期的摘要。"""
        with self.session() as db:
            row = db.scalar(
                select(DailyDigest).where(DailyDigest.digest_date == digest_date)
            )
            if row is None:
                return None

            return {
                "id": row.id,
                "digest_date": row.digest_date.isoformat(),
                "timezone": row.timezone,
                "total_messages": row.total_messages,
                "thai_messages": row.thai_messages,
                "active_users": row.active_users,
                "summary_cn": row.summary_cn,
                "top_topics": row.top_topics,
                "demand_signals": row.demand_signals,
                "keyword_cloud": row.keyword_cloud,
                "hourly_volumes": row.hourly_volumes,
                "created_at": row.created_at.isoformat(),
            }

    def get_recent_digests(self, limit: int = 7) -> List[Dict[str, Any]]:
        """获取最近的每日摘要。"""
        with self.session() as db:
            stmt = (
                select(DailyDigest)
                .order_by(desc(DailyDigest.digest_date))
                .limit(limit)
            )
            rows = list(db.scalars(stmt))

        return [
            {
                "id": row.id,
                "digest_date": row.digest_date.isoformat(),
                "timezone": row.timezone,
                "total_messages": row.total_messages,
                "thai_messages": row.thai_messages,
                "active_users": row.active_users,
                "summary_cn": row.summary_cn,
                "top_topics": row.top_topics,
                "demand_signals": row.demand_signals,
                "keyword_cloud": row.keyword_cloud,
                "hourly_volumes": row.hourly_volumes,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    # MARK: - Analysis Tasks (Async)
    def create_analysis_task(self, task_id: str, mode: str) -> None:
        with self.session() as db:
            db.add(AnalysisTask(task_id=task_id, mode=mode))

    def update_analysis_task(
        self,
        task_id: str,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        result: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        with self.session() as db:
            task = db.get(AnalysisTask, task_id)
            if task is None:
                return
            if status is not None:
                task.status = status
            if progress is not None:
                task.progress = progress
            if result is not None:
                task.result = result
            if error is not None:
                task.error = error
            task.updated_at = datetime.now(timezone.utc)

    def get_analysis_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.session() as db:
            task = db.get(AnalysisTask, task_id)
            if task is None:
                return None
            return {
                "task_id": task.task_id,
                "status": task.status,
                "mode": task.mode,
                "progress": task.progress,
                "result": task.result,
                "error": task.error,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            }

    # MARK: - Message Stats (for dashboard v2)
    def get_hourly_message_volumes(self, window_start: datetime, window_end: datetime) -> List[Dict[str, Any]]:
        """按小时统计消息量。"""
        with self.session() as db:
            stmt = (
                select(
                    func.date_trunc("hour", Message.created_at).label("hour"),
                    func.count().label("count"),
                    func.count().filter(Message.is_thai.is_(True)).label("thai_count"),
                )
                .where(Message.created_at >= window_start)
                .where(Message.created_at < window_end)
                .where(Message.is_deleted.is_(False))
                .group_by(func.date_trunc("hour", Message.created_at))
                .order_by(func.date_trunc("hour", Message.created_at))
            )
            rows = db.execute(stmt).all()

        return [
            {
                "hour": row.hour.isoformat() if row.hour else None,
                "count": int(row.count),
                "thai_count": int(row.thai_count),
            }
            for row in rows
        ]

    def get_active_user_count(self, window_start: datetime, window_end: datetime) -> int:
        """统计时间窗口内的活跃用户数。"""
        with self.session() as db:
            count = db.scalar(
                select(func.count(func.distinct(Message.author_id)))
                .where(Message.created_at >= window_start)
                .where(Message.created_at < window_end)
                .where(Message.is_deleted.is_(False))
            )
            return int(count or 0)

    # MARK: - Compliance
    # 提供按消息/用户删除与保留策略清理能力。
    def compliance_delete(
        self,
        message_ids: Optional[List[int]] = None,
        author_ids: Optional[List[int]] = None,
        hard_delete: bool = False,
    ) -> Dict[str, Any]:
        message_ids = [int(x) for x in (message_ids or [])]
        author_ids = [int(x) for x in (author_ids or [])]
        if not message_ids and not author_ids:
            return {"matched": 0, "affected_window_starts": []}

        now = datetime.now(timezone.utc)

        with self.session() as db:
            condition = None
            if message_ids:
                condition = Message.message_id.in_(message_ids)
            if author_ids:
                author_cond = Message.author_id.in_(author_ids)
                condition = author_cond if condition is None else (condition | author_cond)

            window_rows = db.execute(
                select(Message.created_at).where(condition)
            ).scalars().all()
            affected_windows = sorted(
                {
                    dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                    for dt in window_rows
                    if dt is not None
                }
            )

            if hard_delete:
                matched = db.execute(delete(Message).where(condition)).rowcount or 0
            else:
                matched = (
                    db.execute(
                        update(Message)
                        .where(condition)
                        .values(
                            is_deleted=True,
                            event_type="delete",
                            deleted_at=now,
                            updated_at=now,
                        )
                    ).rowcount
                    or 0
                )

        return {
            "matched": int(matched),
            "affected_window_starts": [x.isoformat() for x in affected_windows],
        }

    def purge_raw_messages(self, retention_days: int, hard_delete: bool = True) -> Dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self.session() as db:
            if hard_delete:
                affected = db.execute(delete(Message).where(Message.created_at < cutoff)).rowcount or 0
            else:
                affected = (
                    db.execute(
                        update(Message)
                        .where(Message.created_at < cutoff)
                        .values(
                            content="[REDACTED]",
                            cleaned_text=None,
                            tokens=[],
                            updated_at=datetime.now(timezone.utc),
                        )
                    ).rowcount
                    or 0
                )

        return {
            "affected_messages": int(affected),
            "retention_days": int(retention_days),
            "hard_delete": bool(hard_delete),
            "cutoff": cutoff.isoformat(),
        }

    # MARK: - Service Heartbeat
    def upsert_service_status(self, service_name: str, status: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        payload = {
            "service_name": service_name,
            "status": status,
            "updated_at": now,
        }

        if self.engine.dialect.name == "postgresql":
            stmt = pg_insert(ServiceHeartbeat).values(**payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=[ServiceHeartbeat.service_name],
                set_={
                    "status": stmt.excluded.status,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            with self.session() as db:
                db.execute(stmt)
            return

        with self.session() as db:
            existing = db.get(ServiceHeartbeat, service_name)
            if existing is None:
                db.add(ServiceHeartbeat(**payload))
                return
            existing.status = status
            existing.updated_at = now

    def get_service_statuses(self) -> List[Dict[str, Any]]:
        with self.session() as db:
            stmt = select(ServiceHeartbeat).order_by(desc(ServiceHeartbeat.updated_at))
            rows = list(db.scalars(stmt))

        return [
            {
                "service_name": row.service_name,
                "status": row.status or {},
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    # MARK: - LLM Provider Credentials
    # 约束：同一时刻只允许一个 provider 为 enabled=true。
    def upsert_llm_provider(
        self,
        provider: str,
        provider_type: str,
        api_key_encrypted: str,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        enabled: bool = True,
    ) -> None:
        provider = provider.strip().lower()
        provider_type = provider_type.strip().lower() if provider_type else "openai_compatible"

        payload = {
            "provider": provider,
            "provider_type": provider_type,
            "api_key_encrypted": api_key_encrypted,
            "base_url": base_url or None,
            "model": model or None,
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc),
        }

        if self.engine.dialect.name == "postgresql":
            with self.session() as db:
                if enabled:
                    db.execute(update(LLMProviderCredential).values(enabled=False))
                stmt = pg_insert(LLMProviderCredential).values(**payload)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[LLMProviderCredential.provider],
                    set_={
                        "provider_type": stmt.excluded.provider_type,
                        "api_key_encrypted": stmt.excluded.api_key_encrypted,
                        "base_url": stmt.excluded.base_url,
                        "model": stmt.excluded.model,
                        "enabled": stmt.excluded.enabled,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                db.execute(stmt)
            return

        with self.session() as db:
            if enabled:
                db.execute(update(LLMProviderCredential).values(enabled=False))
            row = db.get(LLMProviderCredential, provider)
            if row is None:
                db.add(LLMProviderCredential(**payload))
                return
            row.provider_type = provider_type
            row.api_key_encrypted = api_key_encrypted
            row.base_url = base_url or None
            row.model = model or None
            row.enabled = bool(enabled)
            row.updated_at = datetime.now(timezone.utc)

    def list_llm_providers(self) -> List[Dict[str, Any]]:
        with self.session() as db:
            stmt = select(LLMProviderCredential).order_by(desc(LLMProviderCredential.updated_at))
            rows = list(db.scalars(stmt))

        return [
            {
                "provider": row.provider,
                "provider_type": row.provider_type,
                "api_key_encrypted": row.api_key_encrypted,
                "base_url": row.base_url,
                "model": row.model,
                "enabled": row.enabled,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    def set_llm_provider_enabled(self, provider: str, enabled: bool) -> bool:
        provider = provider.strip().lower()
        with self.session() as db:
            row = db.get(LLMProviderCredential, provider)
            if row is None:
                return False
            if enabled:
                db.execute(update(LLMProviderCredential).values(enabled=False))
            row.enabled = bool(enabled)
            row.updated_at = datetime.now(timezone.utc)
            return True

    def delete_llm_provider(self, provider: str) -> bool:
        provider = provider.strip().lower()
        with self.session() as db:
            affected = db.execute(delete(LLMProviderCredential).where(LLMProviderCredential.provider == provider)).rowcount
            return bool(affected)

    def get_active_llm_provider(self) -> Optional[Dict[str, Any]]:
        with self.session() as db:
            stmt = (
                select(LLMProviderCredential)
                .where(LLMProviderCredential.enabled.is_(True))
                .order_by(desc(LLMProviderCredential.updated_at))
                .limit(1)
            )
            row = db.scalar(stmt)
            if row is None:
                return None

            return {
                "provider": row.provider,
                "provider_type": row.provider_type,
                "api_key_encrypted": row.api_key_encrypted,
                "base_url": row.base_url,
                "model": row.model,
                "enabled": row.enabled,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }

    # MARK: - Dashboard Read Models
    def get_dashboard_stats(self, hours: int = 24) -> Dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        with self.session() as db:
            total = db.scalar(select(func.count()).select_from(Message).where(Message.created_at >= cutoff)) or 0
            thai = db.scalar(
                select(func.count()).select_from(Message).where(Message.created_at >= cutoff, Message.is_thai.is_(True))
            ) or 0
            deleted = db.scalar(
                select(func.count()).select_from(Message).where(Message.created_at >= cutoff, Message.is_deleted.is_(True))
            ) or 0
            active_users = db.scalar(
                select(func.count(func.distinct(Message.author_id)))
                .where(Message.created_at >= cutoff)
                .where(Message.is_deleted.is_(False))
            ) or 0
            last_message = db.scalar(select(func.max(Message.created_at)))
            last_analysis = db.scalar(select(func.max(AnalysisRun.window_end)))
            active_llm = db.scalar(
                select(LLMProviderCredential.provider)
                .where(LLMProviderCredential.enabled.is_(True))
                .order_by(desc(LLMProviderCredential.updated_at))
                .limit(1)
            )

        return {
            "hours": hours,
            "total_messages": int(total),
            "thai_messages": int(thai),
            "deleted_messages": int(deleted),
            "active_users": int(active_users),
            "thai_ratio": float((thai / total) * 100) if total else 0.0,
            "last_message_at": last_message.isoformat() if last_message else None,
            "last_analysis_window_end": last_analysis.isoformat() if last_analysis else None,
            "active_llm_provider": active_llm,
            "services": self.get_service_statuses(),
        }

    def get_recent_keywords(self, hours: int = 24, limit: int = 200) -> List[Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self.session() as db:
            stmt = (
                select(HourlyKeyword)
                .where(HourlyKeyword.window_start >= cutoff)
                .order_by(desc(HourlyKeyword.window_start), desc(HourlyKeyword.tfidf_score))
                .limit(limit)
            )
            items = list(db.scalars(stmt))

        return [
            {
                "window_start": item.window_start.isoformat(),
                "window_end": item.window_end.isoformat(),
                "keyword": item.keyword,
                "tfidf_score": item.tfidf_score,
                "frequency": item.frequency,
            }
            for item in items
        ]

    def get_recent_keywords_with_translations(self, hours: int = 24, limit: int = 200) -> List[Dict[str, Any]]:
        """获取最近关键词并附带中文翻译。"""
        keywords = self.get_recent_keywords(hours=hours, limit=limit)
        thai_words = list({kw["keyword"] for kw in keywords})
        translations = self.get_keyword_translations(thai_words)

        for kw in keywords:
            kw["keyword_cn"] = translations.get(kw["keyword"], kw["keyword"])

        return keywords

    def get_recent_analysis_runs(self, limit: int = 24) -> List[Dict[str, Any]]:
        with self.session() as db:
            stmt = select(AnalysisRun).order_by(desc(AnalysisRun.window_start)).limit(limit)
            runs = list(db.scalars(stmt))

        return [
            {
                "id": run.id,
                "window_start": run.window_start.isoformat(),
                "window_end": run.window_end.isoformat(),
                "message_count": run.message_count,
                "keywords": run.keywords,
                "demand_signals": run.demand_signals,
                "summary": run.summary,
                "created_at": run.created_at.isoformat(),
            }
            for run in runs
        ]

    # MARK: - Trend Data
    def get_keyword_trends(self, days: int = 7, limit: int = 20) -> List[Dict[str, Any]]:
        """获取过去 N 天的关键词趋势数据。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        with self.session() as db:
            # 先找出 top 关键词
            top_stmt = (
                select(
                    HourlyKeyword.keyword,
                    func.sum(HourlyKeyword.frequency).label("total_freq"),
                )
                .where(HourlyKeyword.window_start >= cutoff)
                .group_by(HourlyKeyword.keyword)
                .order_by(desc(func.sum(HourlyKeyword.frequency)))
                .limit(limit)
            )
            top_rows = db.execute(top_stmt).all()
            top_keywords = [row.keyword for row in top_rows]

            if not top_keywords:
                return []

            # 按天聚合频次
            daily_stmt = (
                select(
                    HourlyKeyword.keyword,
                    func.date_trunc("day", HourlyKeyword.window_start).label("day"),
                    func.sum(HourlyKeyword.frequency).label("daily_freq"),
                )
                .where(HourlyKeyword.window_start >= cutoff)
                .where(HourlyKeyword.keyword.in_(top_keywords))
                .group_by(HourlyKeyword.keyword, func.date_trunc("day", HourlyKeyword.window_start))
                .order_by(HourlyKeyword.keyword, func.date_trunc("day", HourlyKeyword.window_start))
            )
            daily_rows = db.execute(daily_stmt).all()

        # 组装结果
        from collections import defaultdict
        trend_data: Dict[str, Dict[str, int]] = defaultdict(dict)
        for row in daily_rows:
            day_str = row.day.strftime("%m-%d") if row.day else ""
            trend_data[row.keyword][day_str] = int(row.daily_freq)

        translations = self.get_keyword_translations(top_keywords)

        result = []
        for keyword in top_keywords:
            daily = trend_data.get(keyword, {})
            dates = sorted(daily.keys())
            counts = [daily.get(d, 0) for d in dates]
            total = sum(counts)

            # 简单趋势判断
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

            result.append({
                "keyword_thai": keyword,
                "keyword_cn": translations.get(keyword, keyword),
                "dates": dates,
                "daily_counts": counts,
                "total": total,
                "status": status,
            })

        return result


# MARK: - Singleton Helpers
_db_singleton: Optional[Database] = None


def get_db(database_url: Optional[str] = None) -> Database:
    global _db_singleton
    if _db_singleton is None:
        _db_singleton = Database(database_url=database_url)
    return _db_singleton


def init_db(database_url: Optional[str] = None) -> Database:
    db = get_db(database_url=database_url)
    db.init_tables()
    return db


@contextmanager
def get_session() -> Iterator[Session]:
    db = get_db()
    with db.session() as session:
        yield session
