"""normalize venues events outcomes

Revision ID: df43004f800a
Revises: 89f4b524e9d1
Create Date: 2026-10-30 15:13:29.732278

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from mm_agent.store.models import UTCDateTime


# revision identifiers, used by Alembic.
revision: str = 'df43004f800a'
down_revision: Union[str, Sequence[str], None] = '89f4b524e9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.rename_table("venue_states", "venues")
    with op.batch_alter_table("venues") as batch:
        batch.add_column(
            sa.Column("display_name", sa.String(length=80), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    op.create_table(
        "events",
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("native_id", sa.String(length=160), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("end_time", UTCDateTime(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("venue", "native_id"),
    )
    op.create_table(
        "outcomes",
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("market_id", sa.String(length=160), nullable=False),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("native_id", sa.String(length=200), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("venue", "market_id", "outcome"),
    )
    op.create_index("ix_outcomes_market", "outcomes", ["venue", "market_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_outcomes_market", table_name="outcomes")
    op.drop_table("outcomes")
    op.drop_table("events")
    with op.batch_alter_table("venues") as batch:
        batch.drop_column("enabled")
        batch.drop_column("display_name")
    op.rename_table("venues", "venue_states")
