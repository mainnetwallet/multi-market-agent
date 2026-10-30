"""Per-venue doctor.

Public read and authenticated balance read are always attempted when
credentials exist. A tiny order place/cancel test runs ONLY with
``--i-accept-real-money`` (or Kalshi's demo environment). The build itself never
sends an order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import Settings
from .domain import DoctorReport, DoctorStatus, VenueId
from .events import EventBus
from .store import Store
from .util import configure_logging
from .venues.kalshi import KalshiVenue
from .venues.registry import VenueRegistry


async def diagnose(
    settings: Settings, venues: list[VenueId], *, test_orders: bool, kalshi_demo: bool
) -> list[DoctorReport]:
    settings.ensure_data_dir()
    store = Store(settings.database_url)
    store.create_all()
    bus = EventBus()
    registry = VenueRegistry(settings, store, bus)
    if kalshi_demo:
        registry.adapters[VenueId.KALSHI] = KalshiVenue(settings, base_url=settings.kalshi_demo_base_url)
    await registry.start()
    reports: list[DoctorReport] = []
    try:
        for venue in venues:
            adapter = registry.get(venue)
            if hasattr(adapter, "test_orders"):
                adapter.test_orders = test_orders
            try:
                report = await adapter.doctor()
            except Exception as exc:
                report = DoctorReport(
                    venue=venue,
                    status=DoctorStatus.UNAVAILABLE,
                    reason=f"{type(exc).__name__}: {str(exc)[:240]}",
                )
            registry._ingest(report)
            reports.append(report)
    finally:
        await registry.close()
        store.close()
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove readiness for one or all venues")
    parser.add_argument("--venue", default="all", choices=["all", "polymarket", "kalshi", "limitless"])
    parser.add_argument(
        "--i-accept-real-money",
        action="store_true",
        help="Allow a tiny real order place/cancel test. Off by default.",
    )
    parser.add_argument(
        "--kalshi-demo",
        action="store_true",
        help="Use Kalshi demo base URL and allow the demo order test.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    configure_logging("INFO", json_logs=False)
    settings = Settings()
    venues = list(settings.enabled_venues) if args.venue == "all" else [VenueId(args.venue)]
    test_orders = args.i_accept_real_money or args.kalshi_demo
    if test_orders:
        print(
            "WARNING: order roundtrip test enabled. "
            + ("Kalshi demo URL selected. " if args.kalshi_demo else "REAL MONEY may be used. ")
            + "Order size is the venue minimum at the lowest permitted price; cancellation is attempted immediately.",
            file=sys.stderr,
        )
    reports = asyncio.run(diagnose(settings, venues, test_orders=test_orders, kalshi_demo=args.kalshi_demo))
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in reports], indent=2, default=str))
    else:
        for report in reports:
            print(f"\n=== {report.venue.value.upper()} : {report.status.value} ===")
            if report.reason:
                print(f"reason: {report.reason}")
            for check in report.checks:
                latency = f" ({check.latency_ms} ms)" if check.latency_ms is not None else ""
                print(f"  [{check.status.value:11}] {check.name}{latency}: {check.detail}")
            if not report.checks:
                print("  no checks returned")
        print(
            "\nReadiness is PAPER-safe when public reads pass. LIVE additionally requires "
            "a passing authenticated read and LIVE_<VENUE>=true set by hand."
        )
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
