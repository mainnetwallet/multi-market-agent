"""persistent conversations: one row per chat, messages keyed by (conversation, seq)

Revision ID: c7e2a91f4b30
Revises: b1c4d7e9a02
Create Date: 2026-10-31 07:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from mm_agent.store.models import UTCDateTime


# revision identifiers, used by Alembic.
revision: str = 'c7e2a91f4b30'
down_revision: Union[str, Sequence[str], None] = 'b1c4d7e9a02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("summary_upto", sa.Integer(), nullable=False),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_conversations_owner_updated", "conversations", ["owner", "updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("chat_messages")
    op.drop_index("ix_conversations_owner_updated", table_name="conversations")
    op.drop_table("conversations")
