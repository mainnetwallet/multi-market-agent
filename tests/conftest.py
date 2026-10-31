from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
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


BRIDGE_ADDRESS = "0x9999888877776666555544443333222211110000"


class _BridgeStub:
    """A stub for the bridge: supported assets, a quote, and a withdraw address."""

    def __init__(self, assets: list[dict] | None = None, status_rows: list[dict] | None = None) -> None:
        self.assets = assets or [
            {
                "chainId": "8453",
                "chainName": "Base",
                "token": {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
                "minCheckoutUsd": 2,
            },
            {
                "chainId": "1",
                "chainName": "Ethereum",
                "token": {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
                "minCheckoutUsd": 7,
            },
        ]
        self.status_rows = status_rows or []
        self.quotes: list[dict] = []
        self.withdraws: list[dict] = []

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        if str(url).endswith("/supported-assets"):
            return httpx.Response(200, json={"supportedAssets": self.assets}, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"transactions": self.status_rows}, request=httpx.Request("GET", url))

    async def post(self, url: str, json: dict | None = None, **kwargs: object) -> httpx.Response:
        if str(url).endswith("/quote"):
            self.quotes.append(json or {})
            return httpx.Response(
                200,
                json={"estFeeBreakdown": {"appFeeUsd": 0.25, "gasUsd": 0.1}, "estCheckoutTimeMs": 25000},
                request=httpx.Request("POST", str(url)),
            )
        if str(url).endswith("/withdraw"):
            self.withdraws.append(json or {})
            # The real bridge answers with only these two fields.
            body = {"address": {"evm": BRIDGE_ADDRESS}, "note": "Send funds to these addresses."}
            return httpx.Response(201, json=body, request=httpx.Request("POST", str(url)))
        if str(url).endswith("/deposit"):
            return httpx.Response(
                201, json={"address": {"evm": BRIDGE_ADDRESS}}, request=httpx.Request("POST", str(url))
            )
        return httpx.Response(200, json={}, request=httpx.Request("POST", str(url)))


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> _BridgeStub:
    stub = _BridgeStub()

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> httpx.Response:
            return await stub.get(url, **kwargs)

        async def post(self, url: str, json: dict | None = None, **kwargs: object) -> httpx.Response:
            return await stub.post(url, json=json, **kwargs)

    monkeypatch.setattr("mm_agent.funds.httpx.AsyncClient", lambda *a, **k: _Client())
    return stub
