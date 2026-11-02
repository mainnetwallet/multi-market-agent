"""In-process load test of the live hub (no network, no venues, no orders).

    python scripts/live_load_test.py [--clients 200] [--events 50] [--seconds 20] [--quotes-per-s 2000]

It builds ``--events`` synthetic events, connects ``--clients`` simulated clients (each follows 3 events), feeds
random quotes through ``LiveHub.on_quote`` and runs the same ``flush`` / ``sweep`` the server runs. Three client
kinds: normal readers, slow readers (read every 2 s) and stuck readers (never read). It reports CPU, memory,
patch rates, queue sizes, drops, evictions and the hub-internal latency (quote handed to the hub -> frame taken
by the client). It does NOT measure the network or a venue: those numbers need a real run.

Exit code 1 when a check fails (patch rate above the coalescing limit, queue above the cap, a stuck client not
evicted, memory growing after the load stops).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import random
import resource
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_eventview import pm  # noqa: E402  (a synthetic EventView built by the real builders)

from mm_agent.live.common import FeedStats, Quote  # noqa: E402
from mm_agent.live.hub import ClientSink, LiveHub  # noqa: E402

FLUSH = 0.25
IDLE_SECONDS = 3.0  # shortened from 120 s so eviction is visible in a short run


class StubFeed:
    name = "polymarket_market"

    def __init__(self) -> None:
        self.stats = FeedStats()
        self.stats.connected = True

    def watch(self, assets: Any) -> None: ...

    def unwatch(self, assets: Any) -> None: ...

    def start(self) -> None: ...

    async def stop(self) -> None: ...


def make_view(i: int) -> dict[str, Any]:
    view = copy.deepcopy(pm("candidates"))
    view["event_id"] = f"E{i}"
    for section in view["sections"]:
        for row in section["rows"]:
            n = row["market_id"]
            row["market_id"] = row["row_id"] = f"E{i}{n}"
            row["token_ids"] = [f"E{i}{n}y", f"E{i}{n}n"]
    return view


def rss_mb() -> float:
    with open("/proc/self/statm") as fh:
        return int(fh.read().split()[1]) * resource.getpagesize() / 1e6


class Stats:
    def __init__(self) -> None:
        self.patches = 0
        self.snapshots = 0
        self.latencies: list[float] = []
        self.max_pending = 0
        self.per_second: dict[int, int] = {}


async def reader(sink: ClientSink, stats: Stats, delay: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        frame = await asyncio.wait_for(sink.get(), timeout=5) if not sink.closed else None
        if frame is None:
            return
        now = time.time()
        if frame["type"] == "patch":
            stats.patches += 1
            sec = int(now)
            stats.per_second[sec] = stats.per_second.get(sec, 0) + 1
            with contextlib.suppress(KeyError, TypeError, ValueError):
                stats.latencies.append(now - int(frame["source_ts"]) / 1000)
        elif frame["type"] == "snapshot":
            stats.snapshots += 1
        if delay:
            await asyncio.sleep(delay)


async def main(args: argparse.Namespace) -> int:
    rnd = random.Random(7)
    views = {f"E{i}": make_view(i) for i in range(args.events)}

    async def load(venue: str, ident: str) -> dict[str, Any] | None:
        view = views.get(ident)
        return copy.deepcopy(view) if view else None

    hub = LiveHub(load_view=load, client_idle_seconds=IDLE_SECONDS, max_loads_per_minute=10_000)
    hub.attach(market=StubFeed())
    tokens = [(e, row["token_ids"][0]) for e, v in views.items() for s in v["sections"] for row in s["rows"]]

    stop = asyncio.Event()
    stats = Stats()
    sinks: list[tuple[ClientSink, str]] = []
    tasks: list[asyncio.Task[None]] = []
    n_stuck = max(1, args.clients // 40)
    n_slow = max(1, args.clients // 10)
    for c in range(args.clients):
        kind = "stuck" if c < n_stuck else "slow" if c < n_stuck + n_slow else "normal"
        sink = hub.connect(f"c{c}")
        for ident in rnd.sample(sorted(views), 3):
            await hub.handle(sink, {"op": "subscribe", "venue": "polymarket", "id": ident})
        sinks.append((sink, kind))
        if kind != "stuck":
            tasks.append(asyncio.create_task(reader(sink, stats, 2.0 if kind == "slow" else 0.0, stop)))

    base_rss, cpu0, t0 = rss_mb(), time.process_time(), time.monotonic()
    quotes = 0
    next_flush = next_sweep = t0
    deadline = t0 + args.seconds
    while (now := time.monotonic()) < deadline:
        batch = max(1, int(args.quotes_per_s * 0.01))
        for _ in range(batch):
            _, tok = rnd.choice(tokens)
            mid = Decimal(rnd.randint(5, 95)) / 100
            bid, ask = mid - Decimal("0.01"), mid + Decimal("0.01")
            hub.on_quote(Quote(tok, bid, ask, mid, str(int(time.time() * 1000))))
            quotes += 1
        if now >= next_flush:
            hub.flush()
            next_flush = now + FLUSH
            for sink, _ in sinks:
                stats.max_pending = max(stats.max_pending, sink.pending())
        if now >= next_sweep:
            hub.sweep(now)
            next_sweep = now + 1.0
        await asyncio.sleep(0.01)
    elapsed = time.monotonic() - t0
    cpu = time.process_time() - cpu0
    peak_rss = rss_mb()

    evicted = sum(1 for sink, kind in sinks if kind == "stuck" and sink.closed)
    stop.set()
    for sink, _ in sinks:
        hub.disconnect(sink)
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0.2)
    end_rss = rss_mb()
    m = hub.metrics()

    lat = sorted(stats.latencies)
    p50 = statistics.median(lat) if lat else 0.0
    p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) > 20 else (lat[-1] if lat else 0.0)
    peak_rate = max(stats.per_second.values(), default=0)
    normal = args.clients - n_stuck - n_slow
    print(
        f"clients={args.clients} (normal {normal}, slow {n_slow}, stuck {n_stuck}) "
        f"events={args.events} seconds={elapsed:.1f}"
    )
    print(
        f"quotes in: {quotes} ({quotes / elapsed:.0f}/s)  patches out: {stats.patches} "
        f"({stats.patches / elapsed:.0f}/s)  snapshots: {stats.snapshots}"
    )
    fanout = args.clients * 3 / args.events  # subscribers per event
    print(f"coalescing: {quotes / max(1.0, stats.patches / fanout):.1f} quotes per patch sent to one subscriber")
    print(
        f"peak patches/s over all readers: {peak_rate}  "
        f"(limit {int(normal + n_slow) * 3 * 4} = readers x 3 events x 4/s)"
    )
    print(f"CPU: {cpu:.2f}s over {elapsed:.1f}s = {100 * cpu / elapsed:.1f}% of one core")
    print(f"memory: start {base_rss:.0f} MB, peak {peak_rss:.0f} MB, after disconnect {end_rss:.0f} MB")
    print(f"queue: max frames pending in any sink {stats.max_pending}  dropped (coalesced/overflow) {m['dropped']}")
    print(f"idle eviction: {evicted}/{n_stuck} stuck clients closed (after {IDLE_SECONDS:.0f}s idle)")
    print(
        f"hub-internal latency (quote -> client): p50 {p50 * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms "
        "(no network, no venue)"
    )
    print(f"hub after all clients left: clients={m['clients']} events={m['events']}")

    checks = {
        "patch rate within coalescing limit": peak_rate <= int(normal + n_slow) * 3 * 4,
        "queues capped": stats.max_pending <= 512,
        "stuck clients evicted": evicted == n_stuck,
        "nothing left after disconnect": m["clients"] == 0,
        "memory not growing after load": end_rss <= peak_rss + 5,
    }
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=200)
    parser.add_argument("--events", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--quotes-per-s", type=int, default=2000)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
