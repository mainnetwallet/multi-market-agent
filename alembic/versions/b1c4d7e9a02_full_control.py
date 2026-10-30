"""full control: idempotency, step-up, allowlist, withdrawals, hard ceilings

Revision ID: b1c4d7e9a02
Revises: fed42dc197d2
Create Date: 2026-10-30 20:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from mm_agent.store.models import DecimalString, UTCDateTime


# revision identifiers, used by Alembic.
revision: str = 'b1c4d7e9a02'
down_revision: Union[str, Sequence[str], None] = 'fed42dc197d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("orders", sa.Column("order_type", sa.String(length=8), nullable=False, server_default="GTC"))
    op.add_column("orders", sa.Column("post_only", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("client_ref", sa.String(length=120), nullable=True))
    op.add_column("fills", sa.Column("market_title", sa.Text(), nullable=True))

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=200), primary_key=True),
        sa.Column("scope", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_idempotency_created", "idempotency_keys", ["created_at"])

    op.create_table(
        "stepup_tokens",
        sa.Column("token_hash", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("used_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_stepup_session", "stepup_tokens", ["session_id", "action"])

    op.create_table(
        "withdrawal_allowlist",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("address", sa.String(length=120), nullable=False),
        sa.Column("network", sa.String(length=40), nullable=False),
        sa.Column("asset", sa.String(length=40), nullable=False),
        sa.Column("added_at", UTCDateTime(), nullable=False),
        sa.Column("usable_at", UTCDateTime(), nullable=False),
        sa.Column("removed_at", UTCDateTime(), nullable=True),
    )

    op.create_table(
        "withdrawals",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("amount_usd", DecimalString(40), nullable=False),
        sa.Column("address_id", sa.String(length=32), nullable=False),
        sa.Column("recipient", sa.String(length=120), nullable=False),
        sa.Column("network", sa.String(length=40), nullable=False),
        sa.Column("asset", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="PENDING_HOLD"),
        sa.Column("hold_until", UTCDateTime(), nullable=False),
        sa.Column("bridge_address", sa.String(length=120), nullable=True),
        sa.Column("tx_hash", sa.String(length=120), nullable=True),
        sa.Column("fee_usd", DecimalString(40), nullable=False, server_default="0"),
        sa.Column("eta_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("submitted_at", UTCDateTime(), nullable=True),
        sa.Column("confirmed_at", UTCDateTime(), nullable=True),
    )
    op.create_index("ix_withdrawals_venue_created", "withdrawals", ["venue", "created_at"])

    op.create_table(
        "hard_ceilings",
        sa.Column("name", sa.String(length=60), primary_key=True),
        sa.Column("value", DecimalString(40), nullable=False),
        sa.Column("actor", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("hard_ceilings")
    op.drop_index("ix_withdrawals_venue_created", table_name="withdrawals")
    op.drop_table("withdrawals")
    op.drop_table("withdrawal_allowlist")
    op.drop_index("ix_stepup_session", table_name="stepup_tokens")
    op.drop_table("stepup_tokens")
    op.drop_index("ix_idempotency_created", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_column("fills", "market_title")
    op.drop_column("orders", "client_ref")
    op.drop_column("orders", "post_only")
    op.drop_column("orders", "order_type")
