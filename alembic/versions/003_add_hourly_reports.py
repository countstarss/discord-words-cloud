"""Add hourly reports table

Revision ID: 003_add_hourly_reports
Revises: 002_add_daily_reports
Create Date: 2026-03-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_add_hourly_reports"
down_revision: Union[str, None] = "002_add_daily_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hourly_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("source_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shard_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("window_start", "window_end", "timezone", name="uq_hourly_reports_window"),
    )
    op.create_index("ix_hourly_reports_report_date", "hourly_reports", ["report_date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_hourly_reports_report_date", table_name="hourly_reports")
    op.drop_table("hourly_reports")
