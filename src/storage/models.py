from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# MARK: - Base
class Base(DeclarativeBase):
    pass


# MARK: - Message Domain
# 原始消息与清洗特征同表保存，便于追溯和快速统计。
class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)

    content: Mapped[str] = mapped_column(Text)
    is_thai: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    language: Mapped[str] = mapped_column(String(16), default="unknown")
    lang_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    cleaned_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tokens: Mapped[List[str]] = mapped_column(JSON, default=list)

    event_type: Mapped[str] = mapped_column(String(20), default="create")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # V2: 消息去重与质量评分
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


# MARK: - Aggregation Domain
class HourlyKeyword(Base):
    __tablename__ = "hourly_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    keyword: Mapped[str] = mapped_column(String(255), index=True)
    tfidf_score: Mapped[float] = mapped_column(Float)
    frequency: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    keywords: Mapped[list[dict]] = mapped_column(JSON, default=list)
    demand_signals: Mapped[list[dict]] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


# MARK: - V2: Keyword Translation Cache
# 泰语关键词翻译缓存，避免重复调 LLM 翻译。
class KeywordTranslation(Base):
    __tablename__ = "keyword_translations"

    keyword_thai: Mapped[str] = mapped_column(String(255), primary_key=True)
    keyword_cn: Mapped[str] = mapped_column(String(255))
    keyword_en: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


# MARK: - V2: Daily Digest
# 每日中文摘要，汇总当天所有小时分析结果。
class DailyDigest(Base):
    __tablename__ = "daily_digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    digest_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Bangkok")

    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    thai_messages: Mapped[int] = mapped_column(Integer, default=0)
    active_users: Mapped[int] = mapped_column(Integer, default=0)

    summary_cn: Mapped[str] = mapped_column(Text, default="")
    top_topics: Mapped[list] = mapped_column(JSON, default=list)
    demand_signals: Mapped[list] = mapped_column(JSON, default=list)
    keyword_cloud: Mapped[list] = mapped_column(JSON, default=list)
    hourly_volumes: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


# MARK: - V2: Async Analysis Tasks
# 异步分析任务队列，支持前端轮询进度。
class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    mode: Mapped[str] = mapped_column(String(20))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
    )


# MARK: - Runtime Observability
class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    service_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        index=True,
    )


# MARK: - LLM Credentials
# API Key 按加密字符串存储，避免明文落库。
class LLMProviderCredential(Base):
    __tablename__ = "llm_provider_credentials"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_type: Mapped[str] = mapped_column(String(32), default="openai_compatible")
    api_key_encrypted: Mapped[str] = mapped_column(Text)
    base_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        index=True,
    )


# MARK: - Indexes
Index("idx_hourly_keywords_window_keyword", HourlyKeyword.window_start, HourlyKeyword.keyword, unique=True)
Index("idx_analysis_runs_window", AnalysisRun.window_start, AnalysisRun.window_end)
Index("idx_messages_dedup", Message.content_hash, Message.is_duplicate)
