"""Replace target-language fields with detected-language metadata

Revision ID: 005_detected_language
Revises: 004_scope_fields
Create Date: 2026-03-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005_detected_language"
down_revision: Union[str, None] = "004_scope_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    message_columns = _columns("messages")
    message_indexes = _indexes("messages")
    with op.batch_alter_table("messages") as batch_op:
        if "detected_language" not in message_columns:
            batch_op.add_column(
                sa.Column("detected_language", sa.String(length=32), nullable=False, server_default="unknown")
            )
        if "detected_language_confidence" not in message_columns:
            batch_op.add_column(
                sa.Column("detected_language_confidence", sa.Float(), nullable=False, server_default="0")
            )
    if {"language", "lang_confidence"}.issubset(message_columns):
        op.execute(
            sa.text(
                "UPDATE messages "
                "SET detected_language = COALESCE(NULLIF(language, ''), 'unknown'), "
                "detected_language_confidence = COALESCE(lang_confidence, 0)"
            )
        )
    if "ix_messages_detected_language" not in message_indexes:
        op.create_index("ix_messages_detected_language", "messages", ["detected_language"], unique=False)
    if "ix_messages_is_target_language" in message_indexes:
        op.drop_index("ix_messages_is_target_language", table_name="messages")
    message_columns = _columns("messages")
    with op.batch_alter_table("messages") as batch_op:
        if "is_target_language" in message_columns:
            batch_op.drop_column("is_target_language")
        if "language" in message_columns:
            batch_op.drop_column("language")
        if "lang_confidence" in message_columns:
            batch_op.drop_column("lang_confidence")

    daily_columns = _columns("daily_reports")
    with op.batch_alter_table("daily_reports") as batch_op:
        if "candidate_message_count" not in daily_columns:
            batch_op.add_column(
                sa.Column("candidate_message_count", sa.Integer(), nullable=False, server_default="0")
            )
    daily_columns = _columns("daily_reports")
    if {"candidate_message_count", "target_message_count"}.issubset(daily_columns):
        op.execute(
            sa.text(
                "UPDATE daily_reports "
                "SET candidate_message_count = COALESCE(target_message_count, 0)"
            )
        )
    daily_columns = _columns("daily_reports")
    with op.batch_alter_table("daily_reports") as batch_op:
        if "target_message_count" in daily_columns:
            batch_op.drop_column("target_message_count")

    hourly_columns = _columns("hourly_reports")
    with op.batch_alter_table("hourly_reports") as batch_op:
        if "target_message_count" in hourly_columns:
            batch_op.drop_column("target_message_count")


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(sa.Column("lang_confidence", sa.Float(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("language", sa.String(length=16), nullable=False, server_default="unknown"))
        batch_op.add_column(
            sa.Column("is_target_language", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.execute(
        sa.text(
            "UPDATE messages "
            "SET language = COALESCE(NULLIF(detected_language, ''), 'unknown'), "
            "lang_confidence = COALESCE(detected_language_confidence, 0), "
            "is_target_language = FALSE"
        )
    )
    op.create_index("ix_messages_is_target_language", "messages", ["is_target_language"], unique=False)
    op.drop_index("ix_messages_detected_language", table_name="messages")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("detected_language")
        batch_op.drop_column("detected_language_confidence")

    with op.batch_alter_table("daily_reports") as batch_op:
        batch_op.add_column(sa.Column("target_message_count", sa.Integer(), nullable=False, server_default="0"))
    op.execute(
        sa.text(
            "UPDATE daily_reports "
            "SET target_message_count = COALESCE(candidate_message_count, 0)"
        )
    )
    with op.batch_alter_table("daily_reports") as batch_op:
        batch_op.drop_column("candidate_message_count")

    with op.batch_alter_table("hourly_reports") as batch_op:
        batch_op.add_column(sa.Column("target_message_count", sa.Integer(), nullable=False, server_default="0"))
