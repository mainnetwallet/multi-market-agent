"""Drop the ``mode`` column: every order is real, so there is nothing to record.

Revision ID: e6b3a9d2f417
Revises: d4a8f6c1e203
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e6b3a9d2f417"
down_revision: str | Sequence[str] | None = "d4a8f6c1e203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("positions") as batch:
        batch.drop_constraint("uq_position", type_="unique")
    with op.batch_alter_table("pnl_ledger") as batch:
        batch.drop_index("ix_pnl_created")
    with op.batch_alter_table("rules") as batch:
        batch.drop_index("ix_rules_venue_mode_status")

    for table in ("proposals", "orders", "positions", "pnl_ledger", "rules"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("mode")

    with op.batch_alter_table("positions") as batch:
        batch.create_unique_constraint("uq_position", ["venue", "market_id", "outcome"])
    with op.batch_alter_table("pnl_ledger") as batch:
        batch.create_index("ix_pnl_created", ["venue", "created_at"])
    with op.batch_alter_table("rules") as batch:
        batch.create_index("ix_rules_venue_status", ["venue", "status"])


def downgrade() -> None:
    with op.batch_alter_table("rules") as batch:
        batch.drop_index("ix_rules_venue_status")
    with op.batch_alter_table("pnl_ledger") as batch:
        batch.drop_index("ix_pnl_created")
    with op.batch_alter_table("positions") as batch:
        batch.drop_constraint("uq_position", type_="unique")

    for table in ("proposals", "orders", "positions", "pnl_ledger", "rules"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("mode", sa.String(length=8), nullable=False, server_default="LIVE"))

    with op.batch_alter_table("positions") as batch:
        batch.create_unique_constraint("uq_position", ["venue", "mode", "market_id", "outcome"])
    with op.batch_alter_table("pnl_ledger") as batch:
        batch.create_index("ix_pnl_created", ["venue", "mode", "created_at"])
    with op.batch_alter_table("rules") as batch:
        batch.create_index("ix_rules_venue_mode_status", ["venue", "mode", "status"])
