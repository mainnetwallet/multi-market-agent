"""The Kalshi and Limitless account streams, against local fake servers that speak each venue's protocol.

These prove our side of the protocol (framing, handshake, resubscribe, errors). They cannot prove the real
venues behave the same way; that needs one real account and is called out in the PR.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from mm_agent.venues import account_ws
from mm_agent.venues.account_ws import AccountStreamError, kalshi_account_events, limitless_account_events

Handler = Callable[[ServerConnection], Awaitable[None]]


@asynccontextmanager
async def fake_server(handler: Handler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def first(stream: AsyncIterator[None], timeout: float = 5.0) -> None:
    return await asyncio.wait_for(anext(stream), timeout=timeout)


# ----------------------------------------------------------------------------------------------- Kalshi
async def test_kalshi_subscribes_to_private_channels_and_ticks_on_fills() -> None:
    seen: dict[str, Any] = {}

    async def handler(ws: ServerConnection) -> None:
        seen["key"] = ws.request.headers.get("KALSHI-ACCESS-KEY")
        command = json.loads(await ws.recv())
        seen["command"] = command
        for sid, channel in enumerate(command["params"]["channels"], start=1):
            await ws.send(json.dumps({"id": 1, "type": "subscribed", "msg": {"channel": channel, "sid": sid}}))
        await ws.send(json.dumps({"type": "fill", "sid": 1, "msg": {"trade_id": "t1"}}))
        await ws.wait_closed()

    async with fake_server(handler) as url:
        stream = kalshi_account_events(url, lambda: {"KALSHI-ACCESS-KEY": "key-1"})
        await first(stream)  # catch-up tick after the subscription is acknowledged
        await first(stream)  # the fill
        await stream.aclose()

    assert seen["key"] == "key-1"
    assert seen["command"]["cmd"] == "subscribe"
    assert seen["command"]["params"]["channels"] == ["fill", "user_orders", "market_positions"]


async def test_kalshi_error_message_raises() -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "msg": {"code": 9, "msg": "Authentication required"}}))
        await ws.wait_closed()

    async with fake_server(handler) as url:
        with pytest.raises(AccountStreamError, match="9"):
            await first(kalshi_account_events(url, dict))


async def test_kalshi_unacknowledged_subscription_is_a_failure_not_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(account_ws, "ACK_TIMEOUT_SECONDS", 0.3)

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.wait_closed()  # never answers

    async with fake_server(handler) as url:
        with pytest.raises(AccountStreamError, match="acknowledge"):
            await first(kalshi_account_events(url, dict))


async def test_kalshi_unknown_message_type_still_wakes_the_sync() -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(json.dumps({"type": "subscribed", "msg": {"channel": "fill", "sid": 1}}))
        await ws.send(json.dumps({"type": "some_renamed_type", "msg": {}}))
        await ws.wait_closed()

    async with fake_server(handler) as url:
        stream = kalshi_account_events(url, dict)
        await first(stream)
        await first(stream)
        await stream.aclose()


# -------------------------------------------------------------------------------------------- Limitless
def limitless_handler(
    frames_after_subscribe: list[str], record: dict[str, Any], *, subscribes_wanted: int = 1
) -> Handler:
    async def handler(ws: ServerConnection) -> None:
        record["auth"] = ws.request.headers.get("lmts-api-key")
        record["path"] = ws.request.path
        record["subscribes"] = []
        await ws.send('0{"sid":"abc","pingInterval":25000,"pingTimeout":20000}')
        record["connect"] = await ws.recv()
        await ws.send('40/markets,{"sid":"xyz"}')
        await ws.send("2")  # Engine.IO ping
        # The client may subscribe before or after it answers the ping; both orders are valid.
        while record.get("pong") is None or len(record["subscribes"]) < subscribes_wanted:
            frame = await ws.recv()
            if frame == "3":
                record["pong"] = frame
            elif str(frame).startswith("42"):
                record["subscribes"].append(frame)
                if len(record["subscribes"]) == 1:
                    for extra in frames_after_subscribe:
                        await ws.send(extra)
        await ws.wait_closed()

    return handler


async def test_limitless_handshake_subscribes_to_held_markets_and_ticks_on_positions() -> None:
    record: dict[str, Any] = {}
    handler = limitless_handler(['42/markets,["positions",{"account":"0x1","positions":[]}]'], record)

    async with fake_server(handler) as url:
        stream = limitless_account_events(url, lambda: {"lmts-api-key": "tok"}, lambda: ["m-b", "m-a"])
        await first(stream)  # namespace connected: catch-up tick
        await first(stream)  # the positions event
        await stream.aclose()

    assert record["auth"] == "tok"
    assert record["connect"] == "40/markets,"
    assert record["pong"] == "3"
    assert record["subscribes"] == ['42/markets,["subscribe_positions",{"marketSlugs":["m-a","m-b"]}]']


async def test_limitless_resubscribes_when_the_held_markets_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(account_ws, "LIMITLESS_RESUBSCRIBE_CHECK_SECONDS", 0.1)
    record: dict[str, Any] = {}
    handler = limitless_handler([], record, subscribes_wanted=2)
    held = ["m-a"]

    async def consume(stream: AsyncIterator[None]) -> None:
        async for _ in stream:
            pass

    async with fake_server(handler) as url:
        task = asyncio.create_task(consume(limitless_account_events(url, dict, lambda: held)))
        await asyncio.sleep(0.5)
        held.append("m-b")
        for _ in range(40):
            if len(record.get("subscribes", [])) == 2:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert record["subscribes"][0].endswith('{"marketSlugs":["m-a"]}]')
    assert record["subscribes"][1].endswith('{"marketSlugs":["m-a","m-b"]}]')


async def test_limitless_does_not_subscribe_with_nothing_held() -> None:
    sent: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.send('0{"sid":"abc","pingInterval":25000,"pingTimeout":20000}')
        await ws.recv()
        await ws.send('40/markets,{"sid":"xyz"}')
        try:
            while True:
                sent.append(await asyncio.wait_for(ws.recv(), timeout=0.6))
        except (TimeoutError, Exception):
            return

    async with fake_server(handler) as url:
        stream = limitless_account_events(url, dict, lambda: [])
        await first(stream)
        await asyncio.sleep(0.8)
        await stream.aclose()

    assert sent == []  # an empty subscription is ignored by the venue, so none is sent


async def test_limitless_exception_event_raises() -> None:
    record: dict[str, Any] = {}
    handler = limitless_handler(
        ['42/markets,["exception",{"status":"error","message":"Invalid HMAC authentication"}]'], record
    )

    async with fake_server(handler) as url:
        stream = limitless_account_events(url, dict, lambda: ["m-a"])
        await first(stream)
        with pytest.raises(AccountStreamError, match="Invalid HMAC"):
            await first(stream)


async def test_limitless_namespace_refusal_raises() -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.send('0{"sid":"abc","pingInterval":25000,"pingTimeout":20000}')
        await ws.recv()
        await ws.send('44/markets,{"message":"Unauthorized"}')
        await ws.wait_closed()

    async with fake_server(handler) as url:
        with pytest.raises(AccountStreamError, match="refused"):
            await first(limitless_account_events(url, dict, lambda: []))


def test_socketio_event_parsing() -> None:
    parse = account_ws._socketio_event
    assert parse('42/markets,["positions",{"a":1}]') == ("positions", {"a": 1})
    assert parse('42["system",{"m":"x"}]') == ("system", {"m": "x"})
    assert parse('42/markets,7["orderEvent",{}]') == ("orderEvent", {})
    assert parse("42/markets,not-json") is None


# ------------------------------------------------------------------------------------ adapter wiring
def test_kalshi_stream_url_follows_the_rest_environment(tmp_path) -> None:
    from mm_agent.venues.kalshi import KalshiVenue
    from tests.conftest import make_settings

    prod = KalshiVenue(make_settings(tmp_path))
    assert prod._ws_url() == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    demo = KalshiVenue(make_settings(tmp_path), base_url="https://demo-api.kalshi.co/trade-api/v2")
    assert demo._ws_url() == "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
    custom = KalshiVenue(make_settings(tmp_path, kalshi_ws_url="wss://example.test/trade-api/ws/v2"))
    assert custom._ws_url() == "wss://example.test/trade-api/ws/v2"


def test_limitless_handshake_signature_matches_the_documented_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    import hashlib
    import hmac

    from mm_agent.venues import limitless_signing

    secret = base64.b64encode(b"super-secret").decode()
    monkeypatch.setenv("LMTS_TOKEN_ID", "tok-1")
    monkeypatch.setenv("LMTS_TOKEN_SECRET", secret)
    headers = limitless_signing.ws_auth_headers()

    message = f"{headers['lmts-timestamp']}\nGET\n/socket.io/?EIO=4&transport=websocket\n"
    expected = base64.b64encode(hmac.new(b"super-secret", message.encode(), hashlib.sha256).digest()).decode()
    assert headers["lmts-api-key"] == "tok-1"
    assert headers["lmts-signature"] == expected


def test_limitless_handshake_headers_need_only_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from mm_agent.venues import limitless_signing

    monkeypatch.delenv("LMTS_TOKEN_ID", raising=False)
    monkeypatch.delenv("LMTS_TOKEN_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        limitless_signing.ws_auth_headers()
