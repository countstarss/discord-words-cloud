"""Add report deliveries table for outbound notifications

Revision ID: 006_report_deliveries
Revises: 005_detected_language
Create Date: 2026-04-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006_report_deliveries"
down_revision: Union[str, None] = "005_detected_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "report_deliveries" in inspector.get_table_names():
        return

    op.create_table(
        "report_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("delivery_channel", sa.String(length=32), nullable=False),
        sa.Column("target_key", sa.String(length=128), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False, server_default="global"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_channel",
            "target_key",
            "report_date",
            "scope_key",
            name="uq_report_deliveries_target",
        ),
    )
    op.create_index("ix_report_deliveries_delivery_channel", "report_deliveries", ["delivery_channel"], unique=False)
    op.create_index("ix_report_deliveries_target_key", "report_deliveries", ["target_key"], unique=False)
    op.create_index("ix_report_deliveries_report_date", "report_deliveries", ["report_date"], unique=False)
    op.create_index("ix_report_deliveries_scope_key", "report_deliveries", ["scope_key"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "report_deliveries" not in inspector.get_table_names():
        return

    op.drop_index("ix_report_deliveries_scope_key", table_name="report_deliveries")
    op.drop_index("ix_report_deliveries_report_date", table_name="report_deliveries")
    op.drop_index("ix_report_deliveries_target_key", table_name="report_deliveries")
    op.drop_index("ix_report_deliveries_delivery_channel", table_name="report_deliveries")
    op.drop_table("report_deliveries")
