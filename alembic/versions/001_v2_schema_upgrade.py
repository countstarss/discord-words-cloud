"""V2 schema upgrade: translation cache, daily digests, async tasks, message dedup

Revision ID: 001_v2
Revises: None
Create Date: 2026-03-23
"""

from alembic import op
import sqlalchemy as sa

revision = "001_v2"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- New table: keyword_translations ---
    op.create_table(
        "keyword_translations",
        sa.Column("keyword_thai", sa.String(255), primary_key=True),
        sa.Column("keyword_cn", sa.String(255), nullable=False),
        sa.Column("keyword_en", sa.String(255), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- New table: daily_digests ---
    op.create_table(
        "daily_digests",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("digest_date", sa.DateTime(timezone=True), nullable=False, unique=True),
        sa.Column("timezone", sa.String(64), server_default="Asia/Bangkok"),
        sa.Column("total_messages", sa.Integer, server_default="0"),
        sa.Column("thai_messages", sa.Integer, server_default="0"),
        sa.Column("active_users", sa.Integer, server_default="0"),
        sa.Column("summary_cn", sa.Text, server_default=""),
        sa.Column("top_topics", sa.JSON, server_default="[]"),
        sa.Column("demand_signals", sa.JSON, server_default="[]"),
        sa.Column("keyword_cloud", sa.JSON, server_default="[]"),
        sa.Column("hourly_volumes", sa.JSON, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_daily_digests_digest_date", "daily_digests", ["digest_date"])

    # --- New table: analysis_tasks ---
    op.create_table(
        "analysis_tasks",
        sa.Column("task_id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("progress", sa.Integer, server_default="0"),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_analysis_tasks_status", "analysis_tasks", ["status"])

    # --- Alter table: messages — add dedup & quality columns ---
    op.add_column("messages", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("messages", sa.Column("is_duplicate", sa.Boolean, server_default=sa.text("false")))
    op.add_column("messages", sa.Column("quality_score", sa.Float, server_default=sa.text("1.0")))

    op.create_index("ix_messages_content_hash", "messages", ["content_hash"])
    op.create_index("ix_messages_is_duplicate", "messages", ["is_duplicate"])
    op.create_index("idx_messages_dedup", "messages", ["content_hash", "is_duplicate"])


def downgrade() -> None:
    op.drop_index("idx_messages_dedup", table_name="messages")
    op.drop_index("ix_messages_is_duplicate", table_name="messages")
    op.drop_index("ix_messages_content_hash", table_name="messages")
    op.drop_column("messages", "quality_score")
    op.drop_column("messages", "is_duplicate")
    op.drop_column("messages", "content_hash")

    op.drop_table("analysis_tasks")
    op.drop_table("daily_digests")
    op.drop_table("keyword_translations")
