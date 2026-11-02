"""Read-only check of the live feeds against the REAL Polymarket servers (run once on a real machine).

    python scripts/live_hub_check.py --slug <polymarket-event-slug> [--seconds 30]

It reads the event from public Gamma, follows its YES tokens on the market WebSocket and the sports WebSocket,
and prints what arrives (counts, a few quotes, any game message). No credentials, no orders, nothing is saved.
Compare the output with the protocol notes in ``docs/live-views/00_CONTEXT.md`` and record any difference in
``DOCS_FINDINGS.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_agent.live.common import Quote
from mm_agent.live.polymarket_market_ws import PolymarketMarketFeed
from mm_agent.live.polymarket_sports_ws import GameState, PolymarketSportsFeed


async def main(slug: str, seconds: float) -> int:
    async with httpx.AsyncClient(timeout=15) as http:
        rows = (await http.get("https://gamma-api.polymarket.com/events", params={"slug": slug})).json()
    if not rows:
        print("event not found")
        return 1
    tokens = []
    for market in rows[0].get("markets") or []:
        ids = market.get("clobTokenIds")
        ids = json.loads(ids) if isinstance(ids, str) else ids
        if ids:
            tokens.append(str(ids[0]))
    print(f"following {len(tokens)} YES tokens for {seconds:.0f}s")
    quotes: list[Quote] = []
    games: list[GameState] = []
    market = PolymarketMarketFeed(on_quote=quotes.append, on_state=lambda n, up: print(n, "up" if up else "down"))
    sports = PolymarketSportsFeed(on_game=games.append, on_state=lambda n, up: print(n, "up" if up else "down"))
    market.watch(tokens)
    market.start()
    sports.start()
    await asyncio.sleep(seconds)
    await market.stop()
    await sports.stop()
    print("market:", market.stats.as_dict())
    print("sports:", sports.stats.as_dict())
    for q in quotes[:5]:
        print("quote", q.asset[:10], "bid", q.bid, "ask", q.ask, "last", q.last, "ts", q.source_ts)
    for g in games[:3]:
        print("game", g)
    return 0 if quotes else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.slug, args.seconds)))
