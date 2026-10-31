"""Venue adapter protocol and shared HTTP plumbing.

Adapters are the only place that knows a venue's wire format. Everything above
them uses the normalized domain types.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Generic, TypeVar

import httpx
from pydantic import BaseModel

from ..config import Settings
from ..domain import (
    Balance,
    BookSnapshot,
    CancelResult,
    DoctorReport,
    ExecutionStatus,
    FeeQuote,
    Market,
    MarketRef,
    OrderRequest,
    OrderResult,
    Side,
    VenueCaps,
    VenueId,
    VenueOrder,
    VenueTrade,
)

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None


class MarketQuery(BaseModel):
    q: str | None = None
    category: str | None = None
    limit: int = 40
    cursor: str | None = None
    include_closed: bool = False


class Venue(ABC):
    id: ClassVar[VenueId]
    caps: ClassVar[VenueCaps]

    @property
    @abstractmethod
    def display_name(self) -> str: ...

    @abstractmethod
    async def doctor(self) -> DoctorReport: ...

    @abstractmethod
    async def list_markets(self, query: MarketQuery) -> Page[Market]: ...

    @abstractmethod
    async def get_market(self, ref: MarketRef) -> Market: ...

    @abstractmethod
    async def get_book(self, ref: MarketRef, depth: int = 20) -> BookSnapshot: ...

    @abstractmethod
    def subscribe_books(self, refs: Sequence[MarketRef]) -> AsyncIterator[BookSnapshot]: ...

    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_positions(self) -> list[Any]: ...

    @abstractmethod
    async def get_open_orders(self, ref: MarketRef | None = None) -> list[VenueOrder]: ...

    async def get_account_snapshot(self) -> tuple[list[Any], list[VenueOrder], bool, bool]:
        """Everything the venue says the account holds right now.

        Returns ``(positions, open_orders, positions_complete, orders_complete)``. The two flags say
        whether the lists are the whole account; a venue that paged past what it could read reports
        ``False`` so the caller never treats a missing row as closed. Adapters override this when
        they can read the full account in fewer calls or know about paging.
        """

        positions = await self.get_positions()
        orders = await self.get_open_orders()
        return positions, orders, True, True

    def account_events(self) -> AsyncIterator[None] | None:
        """Optional native push: an iterator that yields each time the account changes.

        ``None`` (the default) means the venue has no account stream and the caller polls. A yield
        carries no data; it only says "read the account again now".
        """

        return None

    async def get_recent_trades(self, ref: MarketRef) -> list[VenueTrade]:
        """Terminal fills for reconciliation. Adapters override when supported."""

        del ref
        return []

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> CancelResult: ...

    @abstractmethod
    async def cancel_all(self, ref: MarketRef | None = None) -> CancelResult: ...

    @abstractmethod
    def fee(self, price: Decimal, size: Decimal, side: Side, market: Market) -> FeeQuote: ...

    async def amend_order(self, order_id: str, *, price: Decimal | None, size: Decimal | None) -> OrderResult:
        """Amend a resting order. Venues without amend keep the default refusal."""

        del order_id, price, size
        return OrderResult(
            venue=self.id,
            client_order_id="",
            status=ExecutionStatus.FAILED,
            requested_size=Decimal("0"),
            message="this venue does not support amending orders",
        )

    async def get_price_history(self, market_id: str, *, interval: str = "1d") -> list[dict[str, Any]]:
        """Venue price history. Venues without one return an empty list."""

        del market_id, interval
        return []

    async def get_funds_history(self) -> list[dict[str, Any]]:
        """Deposits and withdrawals the venue reports. Empty when unsupported."""

        return []

    async def redeem_positions(self, *, market_id: str, outcome: Any = None) -> dict[str, Any]:
        """Redeem a resolved position. Venues without a route refuse clearly."""

        del market_id, outcome
        return {"supported": False, "reason": "this venue has no redeem route"}

    async def split_merge(self, *, market_id: str, action: str) -> dict[str, Any]:
        """Split a full outcome set or merge it back. Advanced; venues only."""

        del market_id, action
        return {"supported": False, "reason": "this venue has no split/merge route"}

    async def start(self) -> None:  # pragma: no cover - default no-op
        return None

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class HttpVenue(Venue):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(f"{self.display_name} adapter not started")
        return self._client

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                headers={"User-Agent": "multi-market-agent/0.1"},
                follow_redirects=True,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        response = await self.client.get(url, params=params, headers=headers)
        if response.status_code >= 400:
            raise VenueHTTPError(response.status_code, response.text[:400], url)
        return response.json()

    async def post_json(
        self,
        url: str,
        *,
        json: Any,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self.client.post(url, json=json, headers=headers)
        if response.status_code >= 400:
            raise VenueHTTPError(response.status_code, response.text[:400], url)
        return response.json()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self.client.request(method, url, params=params, json=json, headers=headers)

    async def poll_books(self, refs: Sequence[MarketRef], interval: float = 3.0) -> AsyncIterator[BookSnapshot]:
        """Fallback book stream for adapters without a wired WebSocket.

        A real WS feed can replace this without changing the pipeline.
        """

        while True:
            for ref in refs:
                try:
                    yield await self.get_book(ref)
                except Exception:
                    # Feed diagnostics observe errors through the scanner/health.
                    continue
            await asyncio.sleep(interval)


class VenueHTTPError(RuntimeError):
    def __init__(self, status_code: int, body: str, url: str) -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        super().__init__(f"venue HTTP {status_code} at {url}: {body}")


class VenueUnavailableError(RuntimeError):
    """Raised when a venue is not ready or credentials are absent."""


class VenueAuthError(VenueUnavailableError):
    pass
