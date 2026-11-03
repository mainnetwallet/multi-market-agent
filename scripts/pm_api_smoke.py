#!/usr/bin/env python3
"""Call every public Polymarket explorer route once against the real API and save sanitized fixtures.

Run it from a machine that can reach polymarket.com:

    python scripts/pm_api_smoke.py            # all routes that need no input, plus the sample inputs below
    python scripts/pm_api_smoke.py --only gamma  # only routes whose key starts with "gamma"

For each route it prints ``ok`` / ``not available`` / ``error``, and writes the answer (cut to a few
rows, wallet addresses kept because they are public) to ``tests/fixtures/live/polymarket/api/<key>.json``.
A route marked evidence U that answers 404 is reported as not available; its path is never guessed.
It sends no credentials and does not touch the account routes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_agent.polymarket_data import DataAPIError
from mm_agent.polymarket_explorer import ROUTES, PolymarketExplorer

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live" / "polymarket" / "api"
WALLET = "0x983eedfbd75803602e4a6e6ea9aab6dc6b9c6748"  # a public leaderboard wallet used in the docs examples


def trim(value: Any, rows: int = 3) -> Any:
    if isinstance(value, list):
        return [trim(v, rows) for v in value[:rows]]
    if isinstance(value, dict):
        return {k: trim(v, rows) for k, v in value.items()}
    return value


async def discover_inputs(client: httpx.AsyncClient) -> dict[str, str]:
    """A live market gives a real condition id, token id and event id for the routes that need one."""
    out: dict[str, str] = {"address": WALLET, "user": WALLET}
    res = await client.get("https://gamma-api.polymarket.com/markets", params={"limit": 1, "closed": "false"})
    market = res.json()[0]
    out["condition_id"] = market["conditionId"]
    tokens = json.loads(market["clobTokenIds"]) if isinstance(market["clobTokenIds"], str) else market["clobTokenIds"]
    out["token_id"] = out["token_ids"] = tokens[0]
    out["id"] = str(market["id"])
    out["slug"] = market["slug"]
    out["pairs"] = f"{tokens[0]}:BUY"
    out["date"] = "2026-11-01"
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="route key prefix")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=20) as client:
        samples = await discover_inputs(client)
        explorer = PolymarketExplorer(lambda: client)
        bad = 0
        for key, route in ROUTES.items():
            if args.only and not key.startswith(args.only):
                continue
            params: dict[str, str] = {}
            for name in [*route.path_params, *route.needs_any, *route.needs_one, route.body_list]:
                if name and name in samples:
                    params[name] = samples[name]
            if key == "gamma.comments":
                params["parent_entity_id"] = samples["id"]
                params["parent_entity_type"] = "market"
            try:
                result = await explorer.call(key, params)
            except DataAPIError as exc:
                note = "not available" if "not available" in exc.message else f"error {exc.status}"
                bad += 0 if note == "not available" else 1
                print(f"{key:34} {note}: {exc.message[:80]}")
                continue
            except ValueError as exc:
                print(f"{key:34} skipped (needs input): {str(exc)[:70]}")
                continue
            if isinstance(result, bytes):
                (OUT / f"{key}.zip.size").write_text(str(len(result)))
            else:
                (OUT / f"{key}.json").write_text(json.dumps(trim(result), indent=1, sort_keys=True))
            print(f"{key:34} ok")
            await asyncio.sleep(0.2)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
