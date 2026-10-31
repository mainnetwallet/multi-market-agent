"""Remove simulated rows left over from the retired paper mode.

Every order is real now. Rows written while the agent could run on paper
describe fills that never happened, so keeping them would put fake positions
and fake PnL into the risk caps. This deletes them. It is not reversible.

Revision ID: d4a8f6c1e203
Revises: c7e2a91f4b30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a8f6c1e203"
down_revision: str | Sequence[str] | None = "c7e2a91f4b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM fills WHERE order_id IN (SELECT id FROM orders WHERE mode = 'PAPER')"))
    for table in ("orders", "pnl_ledger", "positions", "proposals", "rules"):
        bind.execute(sa.text(f"DELETE FROM {table} WHERE mode = 'PAPER'"))


def downgrade() -> None:
    # Deleted simulated rows cannot be restored.
    pass
