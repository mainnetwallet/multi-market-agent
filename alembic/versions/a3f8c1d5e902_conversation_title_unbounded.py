"""conversation title is unbounded text (no 120 character cap)

Revision ID: a3f8c1d5e902
Revises: a3d9f1c2b7e5
Create Date: 2026-11-02 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3f8c1d5e902"
down_revision: Union[str, Sequence[str], None] = "a3d9f1c2b7e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("title", existing_type=sa.String(length=120), type_=sa.Text(), existing_nullable=False)


def downgrade() -> None:
    # Titles may now be longer than the old column allows; Postgres refuses to narrow the type otherwise.
    op.execute(sa.text("UPDATE conversations SET title = SUBSTR(title, 1, 120) WHERE LENGTH(title) > 120"))
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("title", existing_type=sa.Text(), type_=sa.String(length=120), existing_nullable=False)
