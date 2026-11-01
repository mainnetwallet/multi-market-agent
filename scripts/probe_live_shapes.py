#!/usr/bin/env python
"""P0 probe: capture real public API shapes for the live category views.

Read-only. Public endpoints only, no credentials, never places an order. Saves scrubbed JSON to
``tests/fixtures/live/<venue>/<name>.json`` and prints a findings summary.

    python scripts/probe_live_shapes.py              # probe the real venues
    python scripts/probe_live_shapes.py --synthetic  # hand-built fixtures from the documented shapes

With no network route to the venues the script falls back to synthetic fixtures (marked
``"synthetic": true``) and says which calls the owner must run once on a real machine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
LIMITLESS = "https://api.limitless.exchange"
TIMEOUT = 10.0
PAUSE = 0.4  # polite gap between calls, far below every documented limit

_SECRET_KEY = re.compile(
    r"(secret|passphrase|authorization|api[_-]?key|signature|private|password|cookie|token$)", re.I
)


def scrub(value: Any) -> Any:
    """Drop any key that looks like a credential, recursively. Public ids and prices are kept."""

    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def save(venue: str, name: str, payload: Any, *, root: Path = OUT, synthetic: bool = False) -> Path:
    body = scrub(payload)
    if synthetic:
        body = {"synthetic": True, "data": body}
    path = root / venue / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def keys_of(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        return sorted(payload)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return sorted(payload[0])
    return []


class Prober:
    def __init__(self, client: httpx.AsyncClient, root: Path = OUT) -> None:
        self.client = client
        self.root = root
        self.findings: dict[str, Any] = {}
        self.failures: list[str] = []

    async def get(self, venue: str, name: str, url: str, params: Any = None) -> Any:
        await asyncio.sleep(PAUSE)
        try:
            resp = await self.client.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.failures.append(f"{venue}/{name}: {type(exc).__name__}")
            self.findings[f"{venue}/{name}"] = {"ok": False}
            return None
        save(venue, name, data, root=self.root)
        count = len(data) if isinstance(data, list) else None
        self.findings[f"{venue}/{name}"] = {"ok": True, "count": count, "keys": keys_of(data)}
        return data

    async def post(self, venue: str, name: str, url: str, body: Any) -> Any:
        await asyncio.sleep(PAUSE)
        try:
            resp = await self.client.post(url, json=body, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.failures.append(f"{venue}/{name}: {type(exc).__name__}")
            return None
        save(venue, name, data, root=self.root)
        self.findings[f"{venue}/{name}"] = {"ok": True, "keys": keys_of(data)}
        return data


def _events(data: Any) -> list[dict[str, Any]]:
    rows = data.get("events") if isinstance(data, dict) else data
    return [r for r in rows or [] if isinstance(r, dict)]


async def probe_polymarket(p: Prober) -> None:
    sports = await p.get("polymarket", "sports", f"{GAMMA}/sports")
    by_sport: dict[str, str] = {}
    for row in sports if isinstance(sports, list) else []:
        tags = str(row.get("tags") or "").split(",")
        if row.get("sport") and tags and tags[-1].strip().isdigit():
            by_sport[str(row["sport"]).lower()] = tags[-1].strip()
    for sport in ("epl", "ucl", "nba", "nfl", "mlb", "atp", "ufc", "cs2"):
        tag = by_sport.get(sport)
        if not tag:
            continue
        listing = await p.get(
            "polymarket", f"{sport}_events", f"{GAMMA}/events/keyset", {"tag_id": tag, "closed": "false", "limit": 5}
        )
        for ev in _events(listing)[:1]:
            game_id = ev.get("gameId") or (ev.get("markets") or [{}])[0].get("gameId")
            if game_id:
                await p.get("polymarket", f"{sport}_game_{game_id}", f"{GAMMA}/events/keyset", {"game_id": game_id})
    slug = "anthropic-ipo-closing-market-cap-higher-strikes"
    bracket = await p.get("polymarket", "bracket_anthropic_ipo", f"{GAMMA}/events/slug/{slug}")
    if isinstance(bracket, dict):
        p.findings["polymarket/bracket_link_fields"] = sorted(
            k for k in bracket if k in ("series", "seriesSlug", "tags", "relatedEvents", "eventGroup", "parentEvent")
        )
    for ver, label in (({}, "plain"), ({"version": ["v1", "v2"]}, "v1v2")):
        await p.get("polymarket", f"events_{label}", f"{GAMMA}/events", {"closed": "false", "limit": 20, **ver})
        await p.get("polymarket", f"keyset_{label}", f"{GAMMA}/events/keyset", {"closed": "false", "limit": 20, **ver})
    for sub in ("crypto", "5m", "1h"):
        tag = await p.get("polymarket", f"tag_{sub}", f"{GAMMA}/tags/slug/{sub}")
        if isinstance(tag, dict) and tag.get("id"):
            await p.get("polymarket", f"events_{sub}", f"{GAMMA}/events/keyset", {"tag_id": tag["id"], "limit": 5})
    await p.get("polymarket", "sports_market_types", f"{GAMMA}/sports/market-types")


async def probe_kalshi(p: Prober) -> None:
    await p.get(
        "kalshi", "events_open", f"{KALSHI}/events", {"with_nested_markets": "true", "status": "open", "limit": 20}
    )
    await p.get("kalshi", "milestones", f"{KALSHI}/milestones", {"limit": 20})
    miles = p.findings.get("kalshi/milestones", {})
    if miles.get("ok"):
        data = json.loads((p.root / "kalshi" / "milestones.json").read_text(encoding="utf-8"))
        ids = [m.get("id") for m in data.get("milestones", []) if isinstance(m, dict) and m.get("id")][:5]
        if ids:
            await p.get("kalshi", "live_data_batch", f"{KALSHI}/live_data/batch", {"milestone_ids": ",".join(ids)})


async def probe_limitless(p: Prober) -> None:
    active = await p.get("limitless", "markets_active", f"{LIMITLESS}/markets/active", {"limit": 25})
    rows = active.get("data") if isinstance(active, dict) else None
    for row in (rows or [])[:3]:
        slug = row.get("slug") if isinstance(row, dict) else None
        if slug:
            await p.get("limitless", f"history_{slug[:40]}", f"{LIMITLESS}/markets/{slug}/historical-price")


# ---------------------------------------------------------------- synthetic fixtures (documented shapes)


def _pm_market(mid: str, title: str, price: str, **extra: Any) -> dict[str, Any]:
    no = f"{1 - float(price):.2f}"
    return {
        "id": mid,
        "question": title,
        "groupItemTitle": extra.pop("groupItemTitle", title),
        "outcomes": '["Yes", "No"]',
        "outcomePrices": json.dumps([price, no]),
        "clobTokenIds": json.dumps([f"{mid}1", f"{mid}2"]),
        "bestBid": price,
        "bestAsk": f"{float(price) + 0.01:.2f}",
        "oneDayPriceChange": "0.02",
        **extra,
    }


def synthetic_fixtures() -> dict[str, dict[str, Any]]:
    # the away team is listed first in teams[] on purpose: score orientation must use `ordering`
    teams = [
        {"name": "Chelsea", "abbreviation": "CHE", "ordering": "away", "logo": "https://example.invalid/che.png"},
        {"name": "Arsenal", "abbreviation": "ARS", "ordering": "home", "logo": "https://example.invalid/ars.png"},
    ]
    game = {"gameId": 9001, "teams": teams, "score": "2-1", "period": "2H", "elapsed": "63", "live": True}
    soccer_main = {
        "id": "1001",
        "slug": "epl-ars-che-2026-11-01",
        "title": "Arsenal vs. Chelsea",
        **game,
        "markets": [
            _pm_market(
                "m1", "Will Arsenal win?", "0.55", groupItemTitle="Arsenal", sportsMarketType="moneyline", gameId=9001
            ),
            _pm_market(
                "m2", "Will it be a draw?", "0.25", groupItemTitle="Draw", sportsMarketType="moneyline", gameId=9001
            ),
            _pm_market(
                "m3", "Will Chelsea win?", "0.20", groupItemTitle="Chelsea", sportsMarketType="moneyline", gameId=9001
            ),
        ],
    }
    soccer_more = {
        "id": "1002",
        "slug": "epl-ars-che-2026-11-01-more-markets",
        "title": "Arsenal vs. Chelsea - More Markets",
        "gameId": 9001,
        "markets": [
            _pm_market("m4", "Total goals 2.5", "0.60", sportsMarketType="totals", line=2.5, gameId=9001),
            _pm_market("m5", "Both teams to score", "0.58", sportsMarketType="both_teams_to_score", gameId=9001),
        ],
    }
    soccer_half = {
        "id": "1003",
        "slug": "epl-ars-che-2026-11-01-halftime-result",
        "title": "Arsenal vs. Chelsea - Halftime Result",
        "gameId": 9001,
        "markets": [
            _pm_market("m6", "Arsenal leads at half", "0.40", sportsMarketType="first_half_moneyline", gameId=9001)
        ],
    }
    nba = {
        "id": "2001",
        "slug": "nba-lal-bos-2026-11-01",
        "title": "Lakers vs. Celtics",
        "gameId": 9002,
        "score": "88-91",
        "period": "Q3",
        "live": True,
        "teams": [
            {"name": "Lakers", "abbreviation": "LAL", "ordering": "away"},
            {"name": "Celtics", "abbreviation": "BOS", "ordering": "home"},
        ],
        "markets": [
            {
                **_pm_market("n1", "Lakers vs. Celtics", "0.42", sportsMarketType="moneyline", gameId=9002),
                "outcomes": '["Lakers", "Celtics"]',
            },
            _pm_market("n2", "Spread: Celtics (-3.5)", "0.50", sportsMarketType="spreads", line=-3.5, gameId=9002),
            _pm_market(
                "n3", "Lakers vs. Celtics: O/U 214.5", "0.52", sportsMarketType="totals", line=214.5, gameId=9002
            ),
        ],
    }
    strikes = [("b1", "$300B", "0.80"), ("b2", "$400B", "0.62"), ("b3", "$500B", "0.45"), ("b4", "$600B", "0.30")]
    bracket = {
        "id": "3001",
        "slug": "anthropic-ipo-closing-market-cap-higher-strikes",
        "title": "Anthropic IPO Closing Market Cap",
        "markets": [_pm_market(i, f">{t}", pr, groupItemTitle=t) for i, t, pr in reversed(strikes)],  # shuffled
        "tags": [{"id": "10", "slug": "ipos", "label": "IPOs"}],
    }
    candidates = {
        "id": "4001",
        "slug": "synthetic-candidates",
        "title": "Who wins the award?",
        "markets": [
            _pm_market("c1", "Alice", "0.48"),
            _pm_market("c2", "Bob", "0.30"),
            _pm_market("c3", "Cara", "0.12"),
        ],
    }
    updown = {
        "id": "5001",
        "slug": "btc-updown-5m-1793000000",
        "title": "Bitcoin Up or Down - 5 minutes",
        "markets": [{**_pm_market("u1", "BTC up?", "0.51"), "outcomes": '["Up", "Down"]'}],
    }
    binary = {
        "id": "6001",
        "slug": "synthetic-binary",
        "title": "Will it rain?",
        "markets": [_pm_market("y1", "Rain?", "0.35")],
    }
    kalshi_event = {
        "event_ticker": "KXSYN-26NOV01",
        "title": "Synthetic range event",
        "category": "Economics",
        "markets": [
            {"ticker": "KXSYN-26NOV01-T3.0", "floor_strike": 3.0, "yes_bid_dollars": "0.60", "yes_ask_dollars": "0.62"},
            {"ticker": "KXSYN-26NOV01-T3.5", "floor_strike": 3.5, "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.33"},
        ],
    }
    limitless_group = {
        "slug": "synthetic-group",
        "title": "Synthetic group",
        "marketType": "group",
        "markets": [{"slug": "synthetic-group-a", "title": "A", "prices": [0.4, 0.6]}],
    }
    return {
        "polymarket/soccer_main": soccer_main,
        "polymarket/soccer_more_markets": soccer_more,
        "polymarket/soccer_halftime_result": soccer_half,
        "polymarket/nba_game": nba,
        "polymarket/bracket_anthropic_ipo": bracket,
        "polymarket/candidates": candidates,
        "polymarket/crypto_updown_5m": updown,
        "polymarket/binary": binary,
        "kalshi/range_event": kalshi_event,
        "limitless/group": limitless_group,
    }


def write_synthetic(root: Path = OUT) -> list[Path]:
    paths = []
    for key, payload in synthetic_fixtures().items():
        venue, name = key.split("/")
        paths.append(save(venue, name, payload, root=root, synthetic=True))
    return paths


# ---------------------------------------------------------------- entry point


async def run(root: Path = OUT, client: httpx.AsyncClient | None = None) -> Prober:
    own = client is None
    http = client or httpx.AsyncClient(headers={"User-Agent": "mm-agent-probe/1"}, follow_redirects=True)
    prober = Prober(http, root)
    try:
        await probe_polymarket(prober)
        await probe_kalshi(prober)
        await probe_limitless(prober)
    finally:
        if own:
            await http.aclose()
    return prober


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true", help="write hand-built fixtures, no network")
    args = ap.parse_args(argv)
    if args.synthetic:
        paths = write_synthetic()
        print(f"LIVE_VERIFIED=false: wrote {len(paths)} synthetic fixtures under {OUT}")
        return 0
    prober = asyncio.run(run())
    ok = sum(1 for v in prober.findings.values() if isinstance(v, dict) and v.get("ok"))
    print(json.dumps(prober.findings, indent=2, sort_keys=True))
    print(f"\n{ok} calls saved, {len(prober.failures)} failed")
    for line in prober.failures:
        print("  FAILED", line)
    if ok == 0:
        print("No venue reachable: falling back to synthetic fixtures (LIVE_VERIFIED=false).", file=sys.stderr)
        write_synthetic()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
