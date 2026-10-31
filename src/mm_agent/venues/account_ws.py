"""Native account streams for Kalshi and Limitless.

Both only say "something happened on the account": every yield means "read the account now". The event
body is never trusted or parsed for state, so a missed, reordered or unexpected message cannot leave the
app wrong; the poll in ``AccountSync`` stays as the safety net.

* Kalshi: one JSON WebSocket (``wss://.../trade-api/ws/v2``), handshake signed with the REST key headers,
  private channels ``fill``, ``user_orders`` and ``market_positions``.
* Limitless: Socket.IO (Engine.IO v4) over a raw WebSocket, namespace ``/markets``, handshake signed with
  the HMAC headers. ``subscribe_positions`` takes market slugs, so only markets the account already
  holds are covered; a position in a brand-new market is found by the poll.

This module never reads a key. Callers pass a function that returns fresh signed headers, called once per
connection attempt because both venues reject an old timestamp.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from websockets.asyncio.client import connect

from ..util import get_logger

logger = get_logger(__name__)

HeadersFactory = Callable[[], dict[str, str]]

# A subscription that is not acknowledged this fast is treated as failed, not as "quiet".
ACK_TIMEOUT_SECONDS = 15.0
KALSHI_CHANNELS = ("fill", "user_orders", "market_positions")
KALSHI_CONTROL_TYPES = {"subscribed", "unsubscribed", "ok"}
LIMITLESS_NAMESPACE = "/markets"
LIMITLESS_ACCOUNT_EVENTS = {"positions", "orderEvent"}
LIMITLESS_RESUBSCRIBE_CHECK_SECONDS = 2.0


class AccountStreamError(RuntimeError):
    """The venue refused or dropped the stream; the caller backs off and retries."""


# ---------------------------------------------------------------------------------------------- Kalshi
async def kalshi_account_events(url: str, headers: HeadersFactory) -> AsyncIterator[None]:
    """Yield on every private Kalshi message (fill, order or position change)."""

    async with connect(url, additional_headers=headers(), open_timeout=ACK_TIMEOUT_SECONDS) as ws:
        await ws.send(
            json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": list(KALSHI_CHANNELS)}}),
        )
        acked = 0
        deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
        while True:
            timeout = max(0.1, deadline - time.monotonic()) if acked == 0 else None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError as exc:
                raise AccountStreamError("Kalshi did not acknowledge the subscription") from exc
            message = _json(raw)
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "error":
                inner = message.get("msg")
                detail: dict[str, Any] = inner if isinstance(inner, dict) else {}
                raise AccountStreamError(f"Kalshi stream error {detail.get('code')}: {str(detail.get('msg'))[:120]}")
            if kind == "subscribed":
                acked += 1
                if acked == 1:
                    # Catch up on anything that happened while we were not connected.
                    yield None
                continue
            if kind in KALSHI_CONTROL_TYPES:
                continue
            # Only private channels are subscribed, so any other message is an account change. Matching on
            # "anything that is not control" keeps a renamed message type from silently going quiet.
            yield None


# ------------------------------------------------------------------------------------------- Limitless
async def limitless_account_events(
    url: str,
    headers: HeadersFactory,
    slugs: Callable[[], list[str]],
) -> AsyncIterator[None]:
    """Yield on Limitless position/order events for the markets in ``slugs()``.

    ``slugs`` is read again every couple of seconds; when it changes the subscription is re-sent, because
    the server replaces the previous subscription and does not keep it across reconnects.
    """

    async with connect(url, additional_headers=headers(), open_timeout=ACK_TIMEOUT_SECONDS) as ws:
        opened = await asyncio.wait_for(ws.recv(), timeout=ACK_TIMEOUT_SECONDS)
        opened_text = opened.decode() if isinstance(opened, bytes) else opened
        if not opened_text.startswith("0"):
            raise AccountStreamError(f"Limitless sent an unexpected first frame: {opened_text[:40]!r}")
        info = _json(opened_text[1:])
        info = info if isinstance(info, dict) else {}
        # The server pings every pingInterval; silence for longer than interval + timeout means it is gone.
        silence_limit = (float(info.get("pingInterval") or 25000) + float(info.get("pingTimeout") or 20000)) / 1000.0

        await ws.send(f"40{LIMITLESS_NAMESPACE},")
        connected = False
        subscribed: tuple[str, ...] = ()
        last_frame = time.monotonic()
        last_check = 0.0
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=LIMITLESS_RESUBSCRIBE_CHECK_SECONDS)
            except TimeoutError:
                raw = None
            now = time.monotonic()
            if raw is None:
                if now - last_frame > silence_limit:
                    raise AccountStreamError("Limitless stream went silent")
            else:
                last_frame = now
                text = raw.decode() if isinstance(raw, bytes) else raw
                if text == "2":  # Engine.IO ping
                    await ws.send("3")
                elif text.startswith("1"):  # Engine.IO close
                    raise AccountStreamError("Limitless closed the stream")
                elif text.startswith(f"40{LIMITLESS_NAMESPACE}"):
                    connected = True
                    yield None  # catch up on anything missed while disconnected
                elif text.startswith(f"44{LIMITLESS_NAMESPACE}"):
                    raise AccountStreamError(f"Limitless refused the namespace: {text[:120]}")
                elif text.startswith(f"41{LIMITLESS_NAMESPACE}"):
                    raise AccountStreamError("Limitless disconnected the namespace")
                elif text.startswith("42"):
                    event = _socketio_event(text)
                    if event is not None:
                        name, data = event
                        if name in {"exception", "error"}:
                            raise AccountStreamError(f"Limitless {name}: {str(data)[:140]}")
                        if name in LIMITLESS_ACCOUNT_EVENTS:
                            yield None
            if connected and (raw is not None or now - last_check >= LIMITLESS_RESUBSCRIBE_CHECK_SECONDS):
                last_check = now
                wanted = tuple(sorted(set(slugs())))
                if wanted and wanted != subscribed:
                    frame = json.dumps(["subscribe_positions", {"marketSlugs": list(wanted)}], separators=(",", ":"))
                    await ws.send(f"42{LIMITLESS_NAMESPACE},{frame}")
                    subscribed = wanted


# ------------------------------------------------------------------------------------------- helpers
def _json(raw: str | bytes) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _socketio_event(text: str) -> tuple[str, Any] | None:
    """Parse ``42/ns,["event",{...}]`` (a namespace is optional, and so is a numeric ack id)."""

    body = text[2:]
    if body.startswith("/"):
        _, _, body = body.partition(",")
    body = body.lstrip("0123456789")
    payload = _json(body)
    if isinstance(payload, list) and payload and isinstance(payload[0], str):
        return payload[0], payload[1] if len(payload) > 1 else None
    return None
