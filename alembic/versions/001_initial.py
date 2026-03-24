"""Initial migration - create messages table

Revision ID: 001_initial
Revises: 
Create Date: 2024-03-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create messages table
    op.create_table(
        'messages',
        sa.Column('message_id', sa.BigInteger(), primary_key=True),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('author_id', sa.BigInteger(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_target_language', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('language', sa.String(16), nullable=False, server_default='unknown'),
        sa.Column('lang_confidence', sa.Float(), nullable=False, server_default='0'),
        sa.Column('cleaned_text', sa.Text(), nullable=True),
        sa.Column('tokens', postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('event_type', sa.String(20), nullable=False, server_default='create'),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('is_duplicate', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('quality_score', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index('ix_messages_guild_id', 'messages', ['guild_id'], unique=False)
    op.create_index('ix_messages_channel_id', 'messages', ['channel_id'], unique=False)
    op.create_index('ix_messages_author_id', 'messages', ['author_id'], unique=False)
    op.create_index('ix_messages_is_target_language', 'messages', ['is_target_language'], unique=False)
    op.create_index('ix_messages_is_deleted', 'messages', ['is_deleted'], unique=False)
    op.create_index('ix_messages_content_hash', 'messages', ['content_hash'], unique=False)
    op.create_index('ix_messages_is_duplicate', 'messages', ['is_duplicate'], unique=False)
    op.create_index('ix_messages_created_at', 'messages', ['created_at'], unique=False)
    op.create_index('idx_messages_dedup', 'messages', ['content_hash', 'is_duplicate'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_messages_dedup', table_name='messages')
    op.drop_index('ix_messages_created_at', table_name='messages')
    op.drop_index('ix_messages_is_duplicate', table_name='messages')
    op.drop_index('ix_messages_content_hash', table_name='messages')
    op.drop_index('ix_messages_is_deleted', table_name='messages')
    op.drop_index('ix_messages_is_target_language', table_name='messages')
    op.drop_index('ix_messages_author_id', table_name='messages')
    op.drop_index('ix_messages_channel_id', table_name='messages')
    op.drop_index('ix_messages_guild_id', table_name='messages')
    op.drop_table('messages')
