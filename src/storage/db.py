from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional

from sqlalchemy import create_engine, delete, desc, func, inspect, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Base,
    DailyReport,
    HourlyReport,
    Message,
)


# MARK: - Config
def _database_url_from_env() -> str:
    raw_url = os.getenv("DATABASE_URL")
    if raw_url:
        return raw_url

    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "rubii_words")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


# MARK: - Database Gateway
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
        self._ensure_schema_compatibility()

    def _ensure_schema_compatibility(self) -> None:
        inspector = inspect(self.engine)
        table_names = set(inspector.get_table_names())
        if "messages" not in table_names:
            return

        existing_columns = {column["name"] for column in inspector.get_columns("messages")}
        existing_indexes = {index["name"] for index in inspector.get_indexes("messages")}
        daily_columns = {column["name"] for column in inspector.get_columns("daily_reports")} if "daily_reports" in table_names else set()

        alter_statements: list[str] = []
        if "content_hash" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN content_hash VARCHAR(64)")
        if "is_duplicate" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN is_duplicate BOOLEAN NOT NULL DEFAULT FALSE")
        if "quality_score" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN quality_score DOUBLE PRECISION NOT NULL DEFAULT 1.0")
        if "detected_language" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN detected_language VARCHAR(32) NOT NULL DEFAULT 'unknown'")
        if "detected_language_confidence" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE messages ADD COLUMN detected_language_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0"
            )
        if "region_key" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN region_key VARCHAR(64) NOT NULL DEFAULT 'default'")
        if "region_name" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN region_name VARCHAR(128) NOT NULL DEFAULT ''")
        if "channel_name" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN channel_name VARCHAR(255) NOT NULL DEFAULT ''")
        if "channel_group" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN channel_group VARCHAR(64) NOT NULL DEFAULT 'chat'")
        if "scope_key" not in existing_columns:
            alter_statements.append("ALTER TABLE messages ADD COLUMN scope_key VARCHAR(255) NOT NULL DEFAULT 'default:0'")

        index_statements: list[str] = []
        if "content_hash" in existing_columns or any("content_hash" in stmt for stmt in alter_statements):
            if "ix_messages_content_hash" not in existing_indexes:
                index_statements.append("CREATE INDEX IF NOT EXISTS ix_messages_content_hash ON messages (content_hash)")
        if "is_duplicate" in existing_columns or any("is_duplicate" in stmt for stmt in alter_statements):
            if "ix_messages_is_duplicate" not in existing_indexes:
                index_statements.append("CREATE INDEX IF NOT EXISTS ix_messages_is_duplicate ON messages (is_duplicate)")
        if "region_key" in existing_columns or any("region_key" in stmt for stmt in alter_statements):
            if "ix_messages_region_key" not in existing_indexes:
                index_statements.append("CREATE INDEX IF NOT EXISTS ix_messages_region_key ON messages (region_key)")
        if "detected_language" in existing_columns or any("detected_language" in stmt for stmt in alter_statements):
            if "ix_messages_detected_language" not in existing_indexes:
                index_statements.append(
                    "CREATE INDEX IF NOT EXISTS ix_messages_detected_language ON messages (detected_language)"
                )
        if "channel_group" in existing_columns or any("channel_group" in stmt for stmt in alter_statements):
            if "ix_messages_channel_group" not in existing_indexes:
                index_statements.append("CREATE INDEX IF NOT EXISTS ix_messages_channel_group ON messages (channel_group)")
        if "scope_key" in existing_columns or any("scope_key" in stmt for stmt in alter_statements):
            if "ix_messages_scope_key" not in existing_indexes:
                index_statements.append("CREATE INDEX IF NOT EXISTS ix_messages_scope_key ON messages (scope_key)")
        if {"content_hash", "is_duplicate"}.issubset(existing_columns) or (
            any("content_hash" in stmt for stmt in alter_statements)
            and any("is_duplicate" in stmt for stmt in alter_statements)
        ):
            if "idx_messages_dedup" not in existing_indexes:
                index_statements.append(
                    "CREATE INDEX IF NOT EXISTS idx_messages_dedup ON messages (content_hash, is_duplicate)"
                )

        if not alter_statements and not index_statements:
            daily_statements: list[str] = []
        else:
            daily_statements = []

        if "daily_reports" in table_names and "candidate_message_count" not in daily_columns:
            daily_statements.append(
                "ALTER TABLE daily_reports ADD COLUMN candidate_message_count INTEGER NOT NULL DEFAULT 0"
            )

        with self.engine.begin() as conn:
            for statement in alter_statements:
                conn.execute(text(statement))
            for statement in index_statements:
                conn.execute(text(statement))
            for statement in daily_statements:
                conn.execute(text(statement))

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

    def _normalize_report_scope_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(payload)
        normalized.setdefault("scope_type", "global")
        normalized.setdefault("scope_key", "global")
        normalized.setdefault("region_key", "__all__")
        normalized.setdefault("channel_id", 0)
        normalized.setdefault("channel_name", "")
        return normalized

    # MARK: - Message CRUD
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
    def get_messages(
        self,
        window_start: datetime,
        window_end: datetime,
        exclude_duplicates: bool = False,
        min_quality: float = 0.0,
        scope_key: Optional[str] = None,
    ) -> List[Message]:
        with self.session() as db:
            stmt = (
                select(Message)
                .where(Message.is_deleted.is_(False))
                .where(Message.created_at >= window_start)
                .where(Message.created_at < window_end)
            )
            if exclude_duplicates:
                stmt = stmt.where(Message.is_duplicate.is_(False))
            if min_quality > 0:
                stmt = stmt.where(Message.quality_score >= min_quality)
            if scope_key:
                stmt = stmt.where(Message.scope_key == scope_key)
            stmt = stmt.order_by(Message.created_at.asc())
            return list(db.scalars(stmt))

    def get_messages_for_window(
        self,
        window_start: datetime,
        window_end: datetime,
        region_key: Optional[str] = None,
        channel_id: Optional[int] = None,
        channel_group: Optional[str] = None,
        scope_key: Optional[str] = None,
    ) -> List[Message]:
        with self.session() as db:
            stmt = (
                select(Message)
                .where(Message.is_deleted.is_(False))
                .where(Message.created_at >= window_start)
                .where(Message.created_at < window_end)
                .order_by(Message.created_at.asc())
            )
            if region_key:
                stmt = stmt.where(Message.region_key == region_key)
            if channel_id:
                stmt = stmt.where(Message.channel_id == channel_id)
            if channel_group:
                stmt = stmt.where(Message.channel_group == channel_group)
            if scope_key:
                stmt = stmt.where(Message.scope_key == scope_key)
            return list(db.scalars(stmt))

    def get_message_by_id(self, message_id: int) -> Optional[Message]:
        with self.session() as db:
            return db.get(Message, message_id)

    def get_all_messages(
        self,
        limit: int = 100,
        offset: int = 0,
        scope_key: Optional[str] = None,
    ) -> List[Message]:
        with self.session() as db:
            stmt = (
                select(Message)
                .where(Message.is_deleted.is_(False))
                .order_by(desc(Message.created_at))
                .limit(limit)
                .offset(offset)
            )
            if scope_key:
                stmt = stmt.where(Message.scope_key == scope_key)
            return list(db.scalars(stmt))

    def count_messages(
        self,
        scope_key: Optional[str] = None,
    ) -> int:
        with self.session() as db:
            stmt = select(func.count()).select_from(Message).where(Message.is_deleted.is_(False))
            if scope_key:
                stmt = stmt.where(Message.scope_key == scope_key)
            return int(db.scalar(stmt) or 0)

    # MARK: - Daily Report CRUD
    def upsert_daily_report(self, payload: Dict[str, Any]) -> DailyReport:
        payload = self._normalize_report_scope_payload(payload)
        with self.session() as db:
            stmt = (
                select(DailyReport)
                .where(DailyReport.report_date == payload["report_date"])
                .where(DailyReport.timezone == payload["timezone"])
                .where(DailyReport.scope_key == payload["scope_key"])
            )
            existing = db.scalar(stmt)
            if existing is None:
                existing = DailyReport(**payload)
                db.add(existing)
                db.flush()
                return existing

            for key, value in payload.items():
                if key == "id":
                    continue
                setattr(existing, key, value)
            db.flush()
            return existing

    def get_daily_report_by_date(self, report_date: date, scope_key: str = "global") -> Optional[DailyReport]:
        with self.session() as db:
            stmt = (
                select(DailyReport)
                .where(DailyReport.report_date == report_date)
                .where(DailyReport.scope_key == scope_key)
            )
            return db.scalar(stmt)

    def get_all_daily_reports(self, scope_key: str = "global") -> List[DailyReport]:
        with self.session() as db:
            stmt = (
                select(DailyReport)
                .where(DailyReport.scope_key == scope_key)
                .order_by(desc(DailyReport.report_date))
            )
            return list(db.scalars(stmt))

    def count_daily_reports(self, scope_key: str = "global") -> int:
        with self.session() as db:
            stmt = select(func.count()).select_from(DailyReport).where(DailyReport.scope_key == scope_key)
            return int(db.scalar(stmt) or 0)

    # MARK: - Hourly Report CRUD
    def upsert_hourly_report(self, payload: Dict[str, Any]) -> HourlyReport:
        payload = self._normalize_report_scope_payload(payload)
        with self.session() as db:
            stmt = (
                select(HourlyReport)
                .where(HourlyReport.window_start == payload["window_start"])
                .where(HourlyReport.window_end == payload["window_end"])
                .where(HourlyReport.timezone == payload["timezone"])
                .where(HourlyReport.scope_key == payload["scope_key"])
            )
            existing = db.scalar(stmt)
            if existing is None:
                existing = HourlyReport(**payload)
                db.add(existing)
                db.flush()
                return existing

            for key, value in payload.items():
                if key == "id":
                    continue
                setattr(existing, key, value)
            db.flush()
            return existing

    def get_hourly_report_by_window(
        self,
        window_start: datetime,
        window_end: datetime,
        timezone_name: str = "Asia/Shanghai",
        scope_key: str = "global",
    ) -> Optional[HourlyReport]:
        with self.session() as db:
            stmt = (
                select(HourlyReport)
                .where(HourlyReport.window_start == window_start)
                .where(HourlyReport.window_end == window_end)
                .where(HourlyReport.timezone == timezone_name)
                .where(HourlyReport.scope_key == scope_key)
            )
            return db.scalar(stmt)

    def get_hourly_reports_for_date(self, report_date: date, scope_key: str = "global") -> List[HourlyReport]:
        with self.session() as db:
            stmt = (
                select(HourlyReport)
                .where(HourlyReport.report_date == report_date)
                .where(HourlyReport.scope_key == scope_key)
                .order_by(HourlyReport.window_start.asc())
            )
            return list(db.scalars(stmt))

    def count_hourly_reports(self, report_date: Optional[date] = None, scope_key: str = "global") -> int:
        with self.session() as db:
            stmt = select(func.count()).select_from(HourlyReport).where(HourlyReport.scope_key == scope_key)
            if report_date is not None:
                stmt = stmt.where(HourlyReport.report_date == report_date)
            return int(db.scalar(stmt) or 0)

    # MARK: - Stats
    def get_stats(self, hours: int = 24) -> Dict[str, Any]:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        with self.session() as db:
            total = db.scalar(
                select(func.count()).select_from(Message)
                .where(Message.created_at >= cutoff, Message.is_deleted.is_(False))
            ) or 0
            deleted = db.scalar(
                select(func.count()).select_from(Message)
                .where(Message.created_at >= cutoff, Message.is_deleted.is_(True))
            ) or 0
            active_users = db.scalar(
                select(func.count(func.distinct(Message.author_id)))
                .where(Message.created_at >= cutoff)
                .where(Message.is_deleted.is_(False))
            ) or 0
            last_message = db.scalar(select(func.max(Message.created_at)))
            detected_languages = list(
                db.execute(
                    select(Message.detected_language, func.count())
                    .where(Message.created_at >= cutoff, Message.is_deleted.is_(False))
                    .group_by(Message.detected_language)
                    .order_by(func.count().desc(), Message.detected_language.asc())
                    .limit(8)
                )
            )

        return {
            "hours": hours,
            "total_messages": int(total),
            "deleted_messages": int(deleted),
            "active_users": int(active_users),
            "detected_language_breakdown": [
                {"language": str(language or "unknown"), "count": int(count or 0)}
                for language, count in detected_languages
            ],
            "last_message_at": last_message.isoformat() if last_message else None,
        }


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
