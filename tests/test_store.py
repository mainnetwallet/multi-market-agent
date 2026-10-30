from __future__ import annotations

from decimal import Decimal

from mm_agent.domain import (
    Mode,
    Outcome,
    Side,
    VenueId,
)
from mm_agent.store import ORDER_TRANSITIONS, Store


def test_illegal_transition_unknown_to_submitting(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 's.db'}")
    store.create_all()
    order_id = store.create_order(
        venue=VenueId.FAKE,
        mode=Mode.LIVE,
        market_id="m",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.5"),
        size=Decimal("1"),
        client_order_id="client-12345678",
        proposal_id=None,
    )
    assert store.transition_order(order_id, "SUBMITTING")
    assert store.transition_order(order_id, "UNKNOWN", error="timeout")
    assert not store.transition_order(order_id, "SUBMITTING")
    assert store.get_order(order_id)["state"] == "UNKNOWN"
    assert "SUBMITTING" not in ORDER_TRANSITIONS["UNKNOWN"]
    store.close()


def test_fill_updates_position_and_is_idempotent(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 's.db'}")
    store.create_all()
    order_id = store.create_order(
        venue=VenueId.FAKE,
        mode=Mode.PAPER,
        market_id="m",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("10"),
        client_order_id="client-abcdefgh",
        proposal_id=None,
    )
    store.transition_order(order_id, "SUBMITTING")
    assert store.apply_fill(
        order_id=order_id,
        venue=VenueId.FAKE,
        mode=Mode.PAPER,
        market_id="m",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.5"),
        size=Decimal("10"),
        fee_usd=Decimal("0.10"),
        venue_fill_id="f1",
    )
    assert not store.apply_fill(
        order_id=order_id,
        venue=VenueId.FAKE,
        mode=Mode.PAPER,
        market_id="m",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.5"),
        size=Decimal("10"),
        fee_usd=Decimal("0.10"),
        venue_fill_id="f1",
    )
    positions = store.list_positions(mode=Mode.PAPER)
    assert len(positions) == 1
    assert positions[0].shares == Decimal("10")
    assert store.get_order(order_id)["filled"] == Decimal("10")
    store.close()


def test_runtime_lease_is_single_owner(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 's.db'}")
    store.create_all()
    assert store.try_poller_lease("telegram", "a")
    assert store.try_poller_lease("telegram", "a")
    assert not store.try_poller_lease("telegram", "b")
    store.release_poller_lease("telegram", "a")
    assert store.try_poller_lease("telegram", "b")
    store.close()
