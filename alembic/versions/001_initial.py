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
        sa.Column('guild_id', sa.BigInteger(), sa.Index('ix_messages_guild_id')),
        sa.Column('channel_id', sa.BigInteger(), sa.Index('ix_messages_channel_id')),
        sa.Column('author_id', sa.BigInteger(), sa.Index('ix_messages_author_id')),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_target_language', sa.Boolean(), default=False, sa.Index('ix_messages_is_target_language')),
        sa.Column('language', sa.String(16), default='unknown'),
        sa.Column('lang_confidence', sa.Float(), default=0.0),
        sa.Column('cleaned_text', sa.Text(), nullable=True),
        sa.Column('tokens', postgresql.JSON(astext_type=sa.Text()), default=list),
        sa.Column('event_type', sa.String(20), default='create'),
        sa.Column('is_deleted', sa.Boolean(), default=False, sa.Index('ix_messages_is_deleted')),
        sa.Column('content_hash', sa.String(64), nullable=True, sa.Index('ix_messages_content_hash')),
        sa.Column('is_duplicate', sa.Boolean(), default=False, sa.Index('ix_messages_is_duplicate')),
        sa.Column('quality_score', sa.Float(), default=1.0),
        sa.Column('created_at', sa.DateTime(timezone=True), sa.Index('ix_messages_created_at')),
        sa.Column('updated_at', sa.DateTime(timezone=True), default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )
    
    # Create composite index for deduplication
    op.create_index(
        'idx_messages_dedup',
        'messages',
        ['content_hash', 'is_duplicate'],
        unique=False
    )


def downgrade() -> None:
    op.drop_index('idx_messages_dedup', table_name='messages')
    op.drop_table('messages')
