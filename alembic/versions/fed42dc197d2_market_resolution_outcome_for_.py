"""market resolution outcome for calibration

Revision ID: fed42dc197d2
Revises: df43004f800a
Create Date: 2026-10-30 15:34:15.952593

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fed42dc197d2'
down_revision: Union[str, Sequence[str], None] = 'df43004f800a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("markets", sa.Column("resolution_outcome", sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("markets", "resolution_outcome")
