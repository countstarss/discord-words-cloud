# Storage models
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# MARK: - Base
class Base(DeclarativeBase):
    pass


# MARK: - Message Domain
class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    region_key: Mapped[str] = mapped_column(String(64), default="default", index=True)
    region_name: Mapped[str] = mapped_column(String(128), default="")
    channel_name: Mapped[str] = mapped_column(String(255), default="")
    channel_group: Mapped[str] = mapped_column(String(64), default="chat", index=True)
    scope_key: Mapped[str] = mapped_column(String(255), default="default:0", index=True)

    content: Mapped[str] = mapped_column(Text)
    detected_language: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    detected_language_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    cleaned_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tokens: Mapped[List[str]] = mapped_column(JSON, default=list)

    event_type: Mapped[str] = mapped_column(String(20), default="create")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    quality_score: Mapped[float] = mapped_column(Float, default=1.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# MARK: - Indexes
Index("idx_messages_dedup", Message.content_hash, Message.is_duplicate)


# MARK: - Daily Report Domain
class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (
        UniqueConstraint("report_date", "timezone", "scope_key", name="uq_daily_reports_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False, default="global", index=True)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="global", index=True)
    region_key: Mapped[str] = mapped_column(String(64), nullable=False, default="__all__", index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_cn: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# MARK: - Report Delivery Domain
class ReportDelivery(Base):
    __tablename__ = "report_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "delivery_channel",
            "target_key",
            "report_date",
            "scope_key",
            name="uq_report_deliveries_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="global", index=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# MARK: - Hourly Report Domain
class HourlyReport(Base):
    __tablename__ = "hourly_reports"
    __table_args__ = (
        UniqueConstraint("window_start", "window_end", "timezone", "scope_key", name="uq_hourly_reports_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False, default="global", index=True)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="global", index=True)
    region_key: Mapped[str] = mapped_column(String(64), nullable=False, default="__all__", index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shard_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
