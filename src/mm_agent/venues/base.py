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
    FeeQuote,
    Market,
    MarketRef,
    OrderRequest,
    OrderResult,
    Side,
    VenueCaps,
    VenueId,
    VenueOrder,
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

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> CancelResult: ...

    @abstractmethod
    async def cancel_all(self, ref: MarketRef | None = None) -> CancelResult: ...

    @abstractmethod
    def fee(self, price: Decimal, size: Decimal, side: Side, market: Market) -> FeeQuote: ...

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
                headers={"User-Agent": "multi-market-agent/0.1 (+paper-first)"},
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
