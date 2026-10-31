"""Store money columns as NUMERIC on PostgreSQL.

``DecimalString`` used to be a text column on every database. SQLite sums text that
looks like a number, PostgreSQL does not (``function sum(character varying) does not
exist``), so the exposure, daily-loss and withdrawal-limit queries failed there.
On PostgreSQL the type is now an unconstrained NUMERIC. A database created before
that change still has text columns, so convert them here. Values were written with
``format(value, "f")``, so every non-empty value casts exactly. On SQLite this does
nothing: it keeps the text form.

Revision ID: a3d9f1c2b7e5
Revises: e6b3a9d2f417
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from mm_agent.store.models import Base, DecimalString

revision: str = "a3d9f1c2b7e5"
down_revision: str | Sequence[str] | None = "e6b3a9d2f417"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _default_literal(raw: object) -> str | None:
    """Return the plain number inside a reflected column default, or None if there is none."""
    if raw is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(raw))
    return match.group(0) if match else None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"]: column["type"] for column in inspector.get_columns(table.name)}
        defaults = {column["name"]: column.get("default") for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if not isinstance(column.type, DecimalString) or column.name not in existing:
                continue
            if isinstance(existing[column.name], sa.Numeric):
                continue
            # A text column's default (for example '0'::character varying) cannot be cast
            # to numeric automatically, so drop it first and put the same literal back.
            default_literal = _default_literal(defaults.get(column.name))
            if default_literal is not None:
                op.alter_column(table.name, column.name, server_default=None)
            op.alter_column(
                table.name,
                column.name,
                type_=sa.Numeric(),
                existing_type=existing[column.name],
                existing_nullable=column.nullable,
                postgresql_using=f"NULLIF(trim(\"{column.name}\"), '')::numeric",
            )
            if default_literal is not None:
                op.alter_column(
                    table.name,
                    column.name,
                    server_default=sa.text(f"'{default_literal}'"),
                )


def downgrade() -> None:
    # Numeric values convert back to text without loss, but the previous schema could not
    # be used on PostgreSQL anyway, so there is nothing worth restoring.
    pass
