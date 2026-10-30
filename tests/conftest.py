from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from mm_agent.agent import AgentService
from mm_agent.books import BookCache, UnitGuard
from mm_agent.chat import ChatService
from mm_agent.config import Settings
from mm_agent.domain import ResearchReport, Source, VenueId, utcnow
from mm_agent.events import EventBus
from mm_agent.scanner import Scanner
from mm_agent.store import Store
from mm_agent.venues.fake import FakeVenue
from mm_agent.venues.registry import VenueRegistry


@dataclass
class Harness:
    settings: Settings
    store: Store
    bus: EventBus
    registry: VenueRegistry
    scanner: Scanner
    agent: AgentService
    chat: ChatService
    fake: FakeVenue
    guard: UnitGuard


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'test.db'}",
        "enabled_venues_raw": "fake",
        "active_venue": VenueId.FAKE,
        "auto_propose": False,
        "admin_password": "test-password",
        "session_secret": "test-session-secret-long-value",
        "llm_base_url": "",
        "llm_model": "",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> Harness:
    settings = make_settings(tmp_path)
    store = Store(settings.database_url)
    store.create_all()
    bus = EventBus()
    registry = VenueRegistry(settings, store, bus)
    fake = registry.get(VenueId.FAKE)
    assert isinstance(fake, FakeVenue)
    books = BookCache(settings.book_fresh_seconds)
    guard = UnitGuard()
    scanner = Scanner(settings, store, registry, bus)
    agent = AgentService(settings, store, registry, bus, scanner, books, guard, None)
    chat = ChatService(settings, agent)
    await registry.start()
    await registry.refresh_all()
    guard.check_math()
    market = (
        await fake.list_markets(__import__("mm_agent.venues.base", fromlist=["MarketQuery"]).MarketQuery(limit=1))
    ).items[0]
    store.upsert_market(market)
    await guard.run(fake, market)
    yield Harness(settings, store, bus, registry, scanner, agent, chat, fake, guard)
    await registry.close()
    store.close()


@pytest.fixture
def researched(harness: Harness) -> str:
    report = ResearchReport(
        venue=VenueId.FAKE,
        market_id="fed-cut",
        probability=__import__("decimal").Decimal("0.58"),
        summary="The market is underpricing a cut based on two current sources.",
        key_facts=("Inflation cooled", "Two Fed speakers signalled patience"),
        sources=(
            Source(url="https://example.com/a", title="Source A"),
            Source(url="https://example.com/b", title="Source B"),
        ),
        model="test-model",
        created_at=utcnow(),
    )
    rid = harness.store.save_research(
        report.venue,
        report.market_id,
        report.probability,
        report.summary,
        list(report.key_facts),
        [s.model_dump(mode="json") for s in report.sources],
        report.model,
        report.created_at,
    )
    return rid
