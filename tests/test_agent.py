from __future__ import annotations

from decimal import Decimal

import pytest

from mm_agent.domain import MarketRef, Mode, Outcome, RuleKind, RuleStatus, VenueId, utcnow
from mm_agent.rules import RulesEngine
from tests.conftest import Harness


async def setup_tight_market(harness: Harness) -> None:
    market = harness.store.get_market(VenueId.FAKE, "fed-cut")
    assert market is not None
    market = market.model_copy(update={"fee_info": {"taker_rate": "0"}, "tick_size": Decimal("0.001")})
    harness.store.upsert_market(market)
    harness.fake._markets["fed-cut"] = market
    harness.fake.set_book("fed-cut", Outcome.YES, [("0.437", "1000")], [("0.437", "1000")])
    harness.fake.set_book("fed-cut", Outcome.NO, [("0.563", "1000")], [("0.563", "1000")])
    harness.guard.ok = True


@pytest.mark.asyncio
async def test_proposal_is_sourced_and_approved_into_fill(harness: Harness, researched: str) -> None:
    del researched
    await setup_tight_market(harness)
    result = await harness.agent.analyze_market(
        harness.store.get_market(VenueId.FAKE, "fed-cut"), only_outcome=Outcome.YES
    )
    assert result["ok"], result.get("reasons")
    proposal = result["proposal"]
    assert proposal["mode"] == "LIVE"
    assert Decimal(proposal["edge"]) >= Decimal("0.03")
    approved = await harness.agent.approve(proposal["id"], actor="web:test", via="ui")
    assert approved["ok"], approved
    assert approved["status"] in {"FILLED", "PARTIAL"}
    assert approved["venue"] == "fake"
    positions = harness.store.list_positions(mode=Mode.LIVE)
    assert positions
    assert positions[0].shares > 0


@pytest.mark.asyncio
async def test_no_research_means_no_proposal(harness: Harness) -> None:
    result = await harness.agent.analyze_market(
        harness.store.get_market(VenueId.FAKE, "fed-cut"), only_outcome=Outcome.YES
    )
    assert not result["ok"]
    assert "NO_USABLE_RESEARCH" in result["reasons"]
    assert harness.store.list_proposals() == []


@pytest.mark.asyncio
async def test_unknown_order_is_never_resent(harness: Harness, researched: str) -> None:
    del researched
    await setup_tight_market(harness)
    harness.fake.timeout_next = 1
    harness.fake.fill_ratio = Decimal("0.5")
    result = await harness.agent.analyze_market(
        harness.store.get_market(VenueId.FAKE, "fed-cut"), only_outcome=Outcome.YES
    )
    assert result["ok"], result.get("reasons")
    proposal = result["proposal"]
    assert proposal["mode"] == "LIVE"
    approved = await harness.agent.approve(proposal["id"], actor="web:test", via="ui")
    assert approved["status"] == "UNKNOWN"
    order = harness.store.get_order(approved["order_id"])
    assert order is not None and order["state"] == "UNKNOWN"
    # Calling approve again must not place a second order.
    second = await harness.agent.approve(proposal["id"], actor="web:test", via="ui")
    assert not second["ok"]
    assert len([c for c in harness.fake.calls if c[0] == "place_order"]) == 1
    # Reconciliation finds the resting remainder and records the delta fill.
    reconciled = await harness.agent.execution.reconcile_unknown(approved["order_id"])
    assert reconciled["resolved"]
    assert reconciled["state"] == "PARTIAL"
    assert Decimal(reconciled["filled_delta"]) >= 0
    reconciled_order = harness.store.get_order(approved["order_id"])
    assert reconciled_order["state"] == "PARTIAL"
    assert reconciled_order["filled"] > 0
    assert harness.store.list_positions(mode=Mode.LIVE)


@pytest.mark.asyncio
async def test_rule_fires_once_and_stale_book_blocks(harness: Harness, researched: str) -> None:
    del researched
    await setup_tight_market(harness)
    result = await harness.agent.analyze_market(
        harness.store.get_market(VenueId.FAKE, "fed-cut"), only_outcome=Outcome.YES
    )
    assert result["ok"], result.get("reasons")
    approved = await harness.agent.approve(result["proposal"]["id"], actor="web:test", via="ui")
    assert approved["ok"], approved
    position = harness.store.list_positions(mode=Mode.LIVE)[0]
    created = harness.agent.create_exit_rule(
        market_id=position.market_id,
        outcome=position.outcome,
        kind=RuleKind.TAKE_PROFIT,
        trigger_price=Decimal("0.40"),
        actor="web:test",
    )
    assert created["ok"], created
    # Stale book must block, not fire.
    stale = harness.fake._books[MarketRef(venue=VenueId.FAKE, market_id="fed-cut", outcome=Outcome.YES).namespaced]
    harness.fake.replace_book(
        MarketRef(venue=VenueId.FAKE, market_id="fed-cut", outcome=Outcome.YES),
        stale.model_copy(update={"timestamp": utcnow() - __import__("datetime").timedelta(minutes=5)}),
    )
    engine = RulesEngine(
        harness.settings, harness.store, harness.registry, harness.agent.books, harness.agent, harness.bus
    )
    await engine.run_once()
    rule_id = created["rule"]["id"]
    assert harness.store.get_rule(rule_id).status is RuleStatus.BLOCKED
    # Fresh crossing book fires exactly once.
    book = await harness.fake.get_book(MarketRef(venue=VenueId.FAKE, market_id="fed-cut", outcome=Outcome.YES))
    harness.fake.replace_book(
        MarketRef(venue=VenueId.FAKE, market_id="fed-cut", outcome=Outcome.YES),
        book.model_copy(update={"timestamp": utcnow(), "bids": book.bids}),
    )
    before = len(harness.store.list_orders())
    await engine.run_once()
    await engine.run_once()
    after = len(harness.store.list_orders())
    assert after == before + 1
    assert harness.store.get_rule(rule_id).status is RuleStatus.DONE


@pytest.mark.asyncio
async def test_panic_requires_confirmation(harness: Harness) -> None:
    first = await harness.agent.panic(actor="web:test", confirm=False)
    assert not first["ok"]
    assert not harness.agent.paused
    second = await harness.agent.panic(actor="web:test", confirm=True)
    assert second["ok"]
    assert harness.agent.paused


@pytest.mark.asyncio
async def test_unknown_order_blocks_approval(harness: Harness, researched: str) -> None:
    del researched
    from datetime import timedelta

    from mm_agent.domain import Proposal, ProposalStatus, Side, utcnow

    await setup_tight_market(harness)
    harness.fake.timeout_next = 1
    result = await harness.agent.analyze_market(
        harness.store.get_market(VenueId.FAKE, "fed-cut"), only_outcome=Outcome.YES
    )
    assert result["ok"], result.get("reasons")
    first = await harness.agent.approve(result["proposal"]["id"], actor="web:test", via="ui")
    assert first["status"] == "UNKNOWN"
    second_proposal = Proposal(
        venue=VenueId.FAKE,
        mode=Mode.LIVE,
        market_id="fed-cut",
        market_title="Test",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.437"),
        size=Decimal("1"),
        amount_usd=Decimal("0.44"),
        fee_usd=Decimal("0"),
        edge=Decimal("0.05"),
        p_fair=Decimal("0.50"),
        binding_cap="TEST",
        expires_at=utcnow() + timedelta(seconds=300),
    )
    harness.store.create_proposal(second_proposal)
    blocked = await harness.agent.approve(second_proposal.id, actor="web:test", via="ui")
    assert not blocked["ok"]
    assert "UNKNOWN" in blocked["error"]
    assert harness.store.get_proposal(second_proposal.id).status is ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_unknown_reconciles_from_trade_history(harness: Harness, researched: str) -> None:
    del researched
    await setup_tight_market(harness)
    harness.fake.timeout_next = 1
    # Full fill with a timeout leaves no resting order; only trade history can resolve it.
    result = await harness.agent.analyze_market(
        harness.store.get_market(VenueId.FAKE, "fed-cut"), only_outcome=Outcome.YES
    )
    assert result["ok"], result.get("reasons")
    approved = await harness.agent.approve(result["proposal"]["id"], actor="web:test", via="ui")
    assert approved["status"] == "UNKNOWN"
    assert await harness.fake.get_open_orders() == []
    reconciled = await harness.agent.execution.reconcile_unknown(approved["order_id"])
    assert reconciled["resolved"]
    assert reconciled["source"] == "trade_history"
    assert reconciled["state"] in {"FILLED", "PARTIAL"}


@pytest.mark.asyncio
async def test_missing_fee_info_vetoes_entry(harness: Harness, researched: str) -> None:
    del researched
    await setup_tight_market(harness)
    market = harness.store.get_market(VenueId.FAKE, "fed-cut")
    assert market is not None
    market = market.model_copy(update={"fee_info": {}})
    harness.store.upsert_market(market)
    harness.fake._markets["fed-cut"] = market
    result = await harness.agent.analyze_market(market, only_outcome=Outcome.YES)
    assert not result["ok"]
    assert "FEE_INFO_MISSING" in result["reasons"]


@pytest.mark.asyncio
async def test_book_feed_refreshes_cache(harness: Harness) -> None:
    count = await harness.agent.book_feed.run_once()
    assert count == 2
    cached = harness.agent.books.get_cached(MarketRef(venue=VenueId.FAKE, market_id="fed-cut", outcome=Outcome.YES))
    assert cached is not None
    assert harness.agent.book_feed.status()["updates"] >= 2


@pytest.mark.asyncio
async def test_daily_report_calibration_brier(harness: Harness) -> None:
    from mm_agent.domain import MarketStatus
    from mm_agent.reports import daily_report

    market = harness.store.get_market(VenueId.FAKE, "fed-cut")
    assert market is not None
    resolved = market.model_copy(update={"status": MarketStatus.RESOLVED, "resolution_outcome": Outcome.YES})
    harness.store.upsert_market(resolved)
    harness.store.save_probability(
        VenueId.FAKE, "fed-cut", Decimal("0.60"), Decimal("0.70"), Decimal("0.10"), None, True
    )
    report = daily_report(harness.store, harness.agent)
    assert report["calibration"]["status"] == "ok"
    assert report["calibration"]["samples"] == 1
    assert report["calibration"]["brier_score"] == 0.09


@pytest.mark.asyncio
async def test_daily_loss_cap_pauses_entries(harness: Harness) -> None:
    from mm_agent.store.models import PnlRow

    with harness.store.session() as s:
        s.add(
            PnlRow(
                venue="fake",
                mode="LIVE",
                kind="realized",
                amount_usd=Decimal("-6"),
                ref="test",
            )
        )
    result = await harness.agent.analyze_market(
        harness.store.get_market(VenueId.FAKE, "fed-cut"), only_outcome=Outcome.YES
    )
    assert not result["ok"]
    assert "DAILY_LOSS_CAP_AGENT_PAUSED" in result["reasons"]
    assert harness.agent.paused
