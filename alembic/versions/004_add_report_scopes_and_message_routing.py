"""Add report scope fields and message routing metadata

Revision ID: 004_scope_fields
Revises: 003_add_hourly_reports
Create Date: 2026-03-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004_scope_fields"
down_revision: Union[str, None] = "003_add_hourly_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("region_key", sa.String(length=64), nullable=False, server_default="default"))
    op.add_column("messages", sa.Column("region_name", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("messages", sa.Column("channel_name", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("messages", sa.Column("channel_group", sa.String(length=64), nullable=False, server_default="chat"))
    op.add_column("messages", sa.Column("scope_key", sa.String(length=255), nullable=False, server_default="default:0"))
    op.create_index("ix_messages_region_key", "messages", ["region_key"], unique=False)
    op.create_index("ix_messages_channel_group", "messages", ["channel_group"], unique=False)
    op.create_index("ix_messages_scope_key", "messages", ["scope_key"], unique=False)

    op.add_column("daily_reports", sa.Column("scope_type", sa.String(length=20), nullable=False, server_default="global"))
    op.add_column("daily_reports", sa.Column("scope_key", sa.String(length=255), nullable=False, server_default="global"))
    op.add_column("daily_reports", sa.Column("region_key", sa.String(length=64), nullable=False, server_default="__all__"))
    op.add_column("daily_reports", sa.Column("channel_id", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("daily_reports", sa.Column("channel_name", sa.String(length=255), nullable=False, server_default=""))
    op.create_index("ix_daily_reports_scope_type", "daily_reports", ["scope_type"], unique=False)
    op.create_index("ix_daily_reports_scope_key", "daily_reports", ["scope_key"], unique=False)
    op.create_index("ix_daily_reports_region_key", "daily_reports", ["region_key"], unique=False)
    op.create_index("ix_daily_reports_channel_id", "daily_reports", ["channel_id"], unique=False)
    op.drop_constraint("uq_daily_reports_report_date", "daily_reports", type_="unique")
    op.create_unique_constraint("uq_daily_reports_scope", "daily_reports", ["report_date", "timezone", "scope_key"])

    op.add_column("hourly_reports", sa.Column("scope_type", sa.String(length=20), nullable=False, server_default="global"))
    op.add_column("hourly_reports", sa.Column("scope_key", sa.String(length=255), nullable=False, server_default="global"))
    op.add_column("hourly_reports", sa.Column("region_key", sa.String(length=64), nullable=False, server_default="__all__"))
    op.add_column("hourly_reports", sa.Column("channel_id", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("hourly_reports", sa.Column("channel_name", sa.String(length=255), nullable=False, server_default=""))
    op.create_index("ix_hourly_reports_scope_type", "hourly_reports", ["scope_type"], unique=False)
    op.create_index("ix_hourly_reports_scope_key", "hourly_reports", ["scope_key"], unique=False)
    op.create_index("ix_hourly_reports_region_key", "hourly_reports", ["region_key"], unique=False)
    op.create_index("ix_hourly_reports_channel_id", "hourly_reports", ["channel_id"], unique=False)
    op.drop_constraint("uq_hourly_reports_window", "hourly_reports", type_="unique")
    op.create_unique_constraint(
        "uq_hourly_reports_scope",
        "hourly_reports",
        ["window_start", "window_end", "timezone", "scope_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_hourly_reports_scope", "hourly_reports", type_="unique")
    op.create_unique_constraint("uq_hourly_reports_window", "hourly_reports", ["window_start", "window_end", "timezone"])
    op.drop_index("ix_hourly_reports_channel_id", table_name="hourly_reports")
    op.drop_index("ix_hourly_reports_region_key", table_name="hourly_reports")
    op.drop_index("ix_hourly_reports_scope_key", table_name="hourly_reports")
    op.drop_index("ix_hourly_reports_scope_type", table_name="hourly_reports")
    op.drop_column("hourly_reports", "channel_name")
    op.drop_column("hourly_reports", "channel_id")
    op.drop_column("hourly_reports", "region_key")
    op.drop_column("hourly_reports", "scope_key")
    op.drop_column("hourly_reports", "scope_type")

    op.drop_constraint("uq_daily_reports_scope", "daily_reports", type_="unique")
    op.create_unique_constraint("uq_daily_reports_report_date", "daily_reports", ["report_date"])
    op.drop_index("ix_daily_reports_channel_id", table_name="daily_reports")
    op.drop_index("ix_daily_reports_region_key", table_name="daily_reports")
    op.drop_index("ix_daily_reports_scope_key", table_name="daily_reports")
    op.drop_index("ix_daily_reports_scope_type", table_name="daily_reports")
    op.drop_column("daily_reports", "channel_name")
    op.drop_column("daily_reports", "channel_id")
    op.drop_column("daily_reports", "region_key")
    op.drop_column("daily_reports", "scope_key")
    op.drop_column("daily_reports", "scope_type")

    op.drop_index("ix_messages_scope_key", table_name="messages")
    op.drop_index("ix_messages_channel_group", table_name="messages")
    op.drop_index("ix_messages_region_key", table_name="messages")
    op.drop_column("messages", "scope_key")
    op.drop_column("messages", "channel_group")
    op.drop_column("messages", "channel_name")
    op.drop_column("messages", "region_name")
    op.drop_column("messages", "region_key")
