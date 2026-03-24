# Storage models
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
class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)

    content: Mapped[str] = mapped_column(Text)
    is_target_language: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    language: Mapped[str] = mapped_column(String(16), default="unknown")
    lang_confidence: Mapped[float] = mapped_column(Float, default=0.0)

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
