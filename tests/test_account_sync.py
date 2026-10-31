"""A trade made on the venue's own website must reach the app, without a restart and without a delay."""

from __future__ import annotations

from decimal import Decimal

import pytest

from mm_agent.account_sync import AccountSync
from mm_agent.domain import Outcome, Position, Side, VenueId, VenueOrder
from mm_agent.store import Store
from tests.conftest import Harness

D = Decimal
V = VenueId.FAKE


def pos(market: str = "m1", outcome: Outcome = Outcome.YES, shares: str = "10", avg: str = "0.40", **kw) -> Position:
    return Position(venue=V, market_id=market, outcome=outcome, shares=D(shares), avg_price=D(avg), **kw)


def resting(order_id: str = "o1", market: str = "m1", size: str = "10", filled: str = "0") -> VenueOrder:
    return VenueOrder(
        venue=V,
        market_id=market,
        order_id=order_id,
        outcome=Outcome.YES,
        side=Side.BUY,
        price=D("0.40"),
        size=D(size),
        filled=D(filled),
    )


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(f"sqlite:///{tmp_path / 'sync.db'}")
    s.create_all()
    yield s
    s.close()


# --------------------------------------------------------------- positions
def test_position_opened_on_the_website_appears(store: Store) -> None:
    assert store.list_positions(venue=V) == []
    result = store.sync_positions(V, [pos(market_title="Will it rain?")], complete=True)
    assert result["changed"] == [("m1", "YES")]
    (stored,) = store.list_positions(venue=V)
    assert (stored.shares, stored.avg_price, stored.market_title) == (D("10"), D("0.40"), "Will it rain?")


def test_position_sold_on_the_website_disappears(store: Store) -> None:
    store.sync_positions(V, [pos()], complete=True)
    result = store.sync_positions(V, [], complete=True)
    assert ("m1", "YES") in result["moved"]
    assert store.list_positions(venue=V) == []


def test_partial_sell_on_the_website_updates_size(store: Store) -> None:
    store.sync_positions(V, [pos(shares="10")], complete=True)
    store.sync_positions(V, [pos(shares="4")], complete=True)
    (stored,) = store.list_positions(venue=V)
    assert stored.shares == D("4")


def test_incomplete_read_never_closes_a_position(store: Store) -> None:
    """A venue that paged past what we read must not make positions vanish from the app."""

    store.sync_positions(V, [pos(), pos(market="m2")], complete=True)
    store.sync_positions(V, [pos()], complete=False)
    assert {p.market_id for p in store.list_positions(venue=V)} == {"m1", "m2"}


def test_unchanged_read_changes_nothing(store: Store) -> None:
    store.sync_positions(V, [pos(current_price=D("0.5"))], complete=True)
    again = store.sync_positions(V, [pos(current_price=D("0.5"))], complete=True)
    assert again["changed"] == [] and again["moved"] == set() and again["deltas"] == []


def test_price_only_change_is_live_but_not_a_move(store: Store) -> None:
    store.sync_positions(V, [pos(current_price=D("0.5"))], complete=True)
    result = store.sync_positions(V, [pos(current_price=D("0.55"))], complete=True)
    assert result["changed"] == [("m1", "YES")]
    assert result["moved"] == set()


def test_protected_market_is_not_overwritten_or_closed(store: Store) -> None:
    """An order of ours still being settled owns its position; the venue read must not count its fill twice."""

    store.sync_positions(V, [pos(shares="10")], complete=True)
    keep = frozenset({("m1", "YES")})
    store.sync_positions(V, [pos(shares="25")], complete=True, protect=keep)
    assert store.list_positions(venue=V)[0].shares == D("10")
    store.sync_positions(V, [], complete=True, protect=keep)
    assert store.list_positions(venue=V)[0].shares == D("10")


def test_agent_fill_then_venue_read_is_not_counted_twice(store: Store) -> None:
    order_id = store.create_order(
        venue=V,
        market_id="m1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=D("0.40"),
        size=D("10"),
        client_order_id="client-aaaaaaaa",
        proposal_id=None,
    )
    store.transition_order(order_id, "SUBMITTING")
    store.transition_order(order_id, "FILLED", filled=D("10"))
    store.apply_fill(
        order_id=order_id,
        venue=V,
        market_id="m1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=D("0.40"),
        size=D("10"),
        fee_usd=D("0"),
        venue_fill_id="f1",
    )
    store.sync_positions(V, [pos(shares="10", avg="0.40")], complete=True)
    (stored,) = store.list_positions(venue=V)
    assert stored.shares == D("10")


# ------------------------------------------------------------------ orders
def test_order_placed_on_the_website_appears_and_can_be_cancelled_from_the_app(store: Store) -> None:
    touched = store.sync_open_orders(V, [resting()], complete=True, moved=set())
    assert len(touched) == 1
    (order,) = store.list_orders(venue=V)
    assert order["state"] == "OPEN"
    assert order["venue_order_id"] == "o1"
    assert order["client_order_id"].startswith("ext:")
    # Seen again unchanged: nothing to do.
    assert store.sync_open_orders(V, [resting()], complete=True, moved=set()) == []


def test_partial_fill_on_the_website_is_shown(store: Store) -> None:
    store.sync_open_orders(V, [resting()], complete=True, moved=set())
    store.sync_open_orders(V, [resting(filled="4")], complete=True, moved=set())
    (order,) = store.list_orders(venue=V)
    assert (order["state"], order["filled"]) == ("PARTIAL", D("4"))


def test_order_cancelled_on_the_website_is_cancelled(store: Store) -> None:
    store.sync_open_orders(V, [resting()], complete=True, moved=set())
    store.sync_open_orders(V, [], complete=True, moved=set(), settle_after_seconds=-1)
    assert store.list_orders(venue=V)[0]["state"] == "CANCELLED"


def test_order_filled_on_the_website_is_filled_when_the_position_moved(store: Store) -> None:
    store.sync_open_orders(V, [resting()], complete=True, moved=set())
    store.sync_open_orders(V, [], complete=True, moved={("m1", "YES")}, settle_after_seconds=-1)
    order = store.list_orders(venue=V)[0]
    assert (order["state"], order["filled"]) == ("FILLED", D("10"))


def test_unknown_outcome_is_left_alone_when_positions_could_not_be_read(store: Store) -> None:
    store.sync_open_orders(V, [resting()], complete=True, moved=set())
    store.sync_open_orders(V, [], complete=True, moved=None, settle_after_seconds=-1)
    assert store.list_orders(venue=V)[0]["state"] == "OPEN"


def test_just_placed_order_is_not_closed_before_it_settles(store: Store) -> None:
    store.sync_open_orders(V, [resting()], complete=True, moved=set())
    store.sync_open_orders(V, [], complete=True, moved=set())  # default settle window
    assert store.list_orders(venue=V)[0]["state"] == "OPEN"


# ------------------------------------------------------------ the service
async def test_service_picks_up_a_trade_made_on_the_website(harness: Harness) -> None:
    sync = AccountSync(harness.settings, harness.store, harness.registry, harness.bus)
    key = f"{V.value}:fed-cut:YES"
    harness.fake._positions[key] = (D("7"), D("0.31"))  # the user bought on the website

    result = await sync.sync_once(V)

    assert result["positions_changed"] == 1
    (stored,) = harness.store.list_positions(venue=V)
    assert (stored.market_id, stored.outcome, stored.shares) == ("fed-cut", Outcome.YES, D("7"))
    assert any(e.topic == "account.updated" for e in harness.bus.history)

    # Sold it on the website: the next read removes it.
    harness.fake._positions[key] = (D("0"), D("0.31"))
    await sync.sync_once(V)
    assert harness.store.list_positions(venue=V) == []


async def test_service_stays_quiet_when_nothing_changed(harness: Harness) -> None:
    sync = AccountSync(harness.settings, harness.store, harness.registry, harness.bus)
    await sync.sync_once(V)
    before = len([e for e in harness.bus.history if e.topic == "account.updated"])
    again = await sync.sync_once(V)
    after = len([e for e in harness.bus.history if e.topic == "account.updated"])
    assert (again["positions_changed"], again["orders_changed"], again["balance_changed"]) == (0, 0, False)
    assert after == before


async def test_service_reports_a_balance_change(harness: Harness) -> None:
    sync = AccountSync(harness.settings, harness.store, harness.registry, harness.bus)
    await sync.sync_once(V)
    harness.fake._cash = D("940")  # a withdrawal or a trade on the website
    result = await sync.sync_once(V)
    assert result["balance_changed"] is True
    event = [e for e in harness.bus.history if e.topic == "account.updated"][-1]
    assert event.payload["balance"]["available"] == "940"


async def test_trigger_wakes_the_loop_immediately(harness: Harness) -> None:
    """The stream/bus path: an event must start a read now, not at the next poll."""

    import asyncio

    slow = harness.settings.model_copy(update={"account_sync_interval_seconds": 3600.0})  # the clock alone never fires
    sync = AccountSync(slow, harness.store, harness.registry, harness.bus)
    sync.start()
    try:
        await asyncio.sleep(0.2)  # first read done
        harness.fake._positions[f"{V.value}:fed-cut:YES"] = (D("3"), D("0.2"))
        sync.trigger(V)
        for _ in range(40):
            if harness.store.list_positions(venue=V):
                break
            await asyncio.sleep(0.1)
        assert harness.store.list_positions(venue=V)[0].shares == D("3")
    finally:
        await sync.stop()


# ------------------------------------------------- external trades: history + alert
def test_deltas_describe_a_buy_a_partial_sell_and_a_close(store: Store) -> None:
    opened = store.sync_positions(V, [pos(shares="10", avg="0.40", market_title="Rain?")], complete=True)
    (d,) = opened["deltas"]
    assert (d["delta"], d["new"], d["price"], d["title"]) == (D("10"), D("10"), D("0.40"), "Rain?")

    added = store.sync_positions(V, [pos(shares="20", avg="0.50")], complete=True)
    (d,) = added["deltas"]
    assert d["delta"] == D("10") and d["price"] == D("0.60")  # (20*0.5 - 10*0.4) / 10

    sold = store.sync_positions(V, [pos(shares="5", avg="0.50", current_price=D("0.70"))], complete=True)
    (d,) = sold["deltas"]
    assert d["delta"] == D("-15") and d["price"] == D("0.70")

    closed = store.sync_positions(V, [], complete=True)
    (d,) = closed["deltas"]
    assert d["delta"] == D("-5") and d["new"] == D("0")


def test_price_only_change_has_no_delta(store: Store) -> None:
    store.sync_positions(V, [pos(current_price=D("0.5"))], complete=True)
    again = store.sync_positions(V, [pos(current_price=D("0.6"))], complete=True)
    assert again["changed"] and again["deltas"] == []


def test_external_fill_lands_in_history_without_touching_the_position(store: Store) -> None:
    result = store.sync_positions(V, [pos(shares="10", avg="0.40", market_title="Rain?")], complete=True)
    assert store.record_external_fills(V, result["deltas"]) == 1
    (fill,) = store.list_fills(venue=V)
    assert (fill["side"], fill["shares"], fill["market_id"]) == ("BUY", "10", "m1")
    (stored,) = store.list_positions(venue=V)
    assert stored.shares == D("10")  # the position was not counted a second time


async def test_service_records_and_announces_a_website_trade_after_the_baseline(harness: Harness) -> None:
    sync = AccountSync(harness.settings, harness.store, harness.registry, harness.bus)
    key = f"{V.value}:fed-cut:YES"
    harness.fake._positions[key] = (D("3"), D("0.40"))  # already held when the server starts

    await sync.sync_once(V)  # baseline: not reported as a new trade
    assert not any(e.topic == "account.external_change" for e in harness.bus.history)
    assert harness.store.list_fills(venue=V) == []

    harness.fake._positions[key] = (D("8"), D("0.40"))  # bought 5 more on the website
    await sync.sync_once(V)

    external = [e for e in harness.bus.history if e.topic == "account.external_change"]
    assert len(external) == 1
    (trade,) = external[0].payload["trades"]
    assert (trade["side"], trade["shares"], trade["position_after"]) == ("BUY", "5", "8")
    (fill,) = harness.store.list_fills(venue=V)
    assert (fill["side"], fill["shares"]) == ("BUY", "5")
