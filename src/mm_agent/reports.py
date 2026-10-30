"""Daily operating report with a calibration section."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .agent import AgentService
from .domain import Mode
from .store import Store
from .store.models import DecisionRow, FillRow, MarketRow, ProbabilityRow

DHAKA = ZoneInfo("Asia/Dhaka")


def day_bounds(day: datetime | None = None) -> tuple[datetime, datetime]:
    local_now = (day or datetime.now(DHAKA)).astimezone(DHAKA)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=DHAKA)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def daily_report(store: Store, agent: AgentService, day: datetime | None = None) -> dict[str, Any]:
    start, end = day_bounds(day)
    with store.session() as s:
        decisions = s.scalars(
            select(DecisionRow).where(DecisionRow.created_at >= start, DecisionRow.created_at < end)
        ).all()
        fills = s.scalars(select(FillRow).where(FillRow.created_at >= start, FillRow.created_at < end)).all()
        probabilities = s.scalars(
            select(ProbabilityRow).where(ProbabilityRow.created_at >= start, ProbabilityRow.created_at < end)
        ).all()
        resolved = s.scalars(
            select(MarketRow).where(MarketRow.status == "RESOLVED", MarketRow.resolution_outcome.is_not(None))
        ).all()
        calibration_samples: list[tuple[Decimal, int]] = []
        for market_row in resolved:
            latest = s.scalar(
                select(ProbabilityRow)
                .where(
                    ProbabilityRow.venue == market_row.venue,
                    ProbabilityRow.market_id == market_row.native_id,
                )
                .order_by(ProbabilityRow.created_at.desc())
                .limit(1)
            )
            if latest is not None:
                calibration_samples.append((latest.p_fair, 1 if market_row.resolution_outcome == "YES" else 0))
    action_counts = Counter(d.action for d in decisions)
    reasons = Counter(code for d in decisions for code in (d.reason_codes or []))
    notional = sum((f.price * f.size for f in fills), start=Decimal("0"))
    fees = sum((f.fee_usd for f in fills), start=Decimal("0"))
    bins: dict[str, int] = {"0-20%": 0, "20-40%": 0, "40-60%": 0, "60-80%": 0, "80-100%": 0}
    for p in probabilities:
        value = float(p.p_fair)
        if value < 0.2:
            bins["0-20%"] += 1
        elif value < 0.4:
            bins["20-40%"] += 1
        elif value < 0.6:
            bins["40-60%"] += 1
        elif value < 0.8:
            bins["60-80%"] += 1
        else:
            bins["80-100%"] += 1
    if calibration_samples:
        brier = sum(
            ((float(probability) - actual) ** 2 for probability, actual in calibration_samples),
            start=0.0,
        ) / len(calibration_samples)
        calibration = {
            "status": "ok",
            "samples": len(calibration_samples),
            "brier_score": round(brier, 6),
            "note": "Brier score over latest recorded p_fair for resolved markets (lower is better).",
        }
    else:
        calibration = {
            "status": "insufficient_resolved_samples",
            "samples": 0,
            "brier_score": None,
            "note": "A resolved market with a stored probability is required.",
        }
    positions = store.list_positions()
    status = agent.status()
    return {
        "day_dhaka": start.astimezone(DHAKA).date().isoformat(),
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "badge": status["badge"],
        "paused": status["paused"],
        "decisions": dict(action_counts),
        "top_vetoes": reasons.most_common(10),
        "fills": len(fills),
        "fill_notional_usd": str(notional.quantize(Decimal("0.01"))),
        "fees_usd": str(fees.quantize(Decimal("0.01"))),
        "realized_pnl_usd": str(store.today_pnl()),
        "open_positions": len(positions),
        "open_exposure_usd_by_mode": {
            "PAPER": str(store.total_exposure(Mode.PAPER)),
            "LIVE": str(store.total_exposure(Mode.LIVE)),
        },
        "rules": dict(Counter(r.status.value for r in store.list_rules())),
        "probability_bins": bins,
        "calibration": calibration,
    }


def render_report(report: dict[str, Any]) -> str:
    calibration = report["calibration"]
    calibration_text = calibration["status"]
    if calibration.get("brier_score") is not None:
        calibration_text += f" · Brier {calibration['brier_score']} · n={calibration['samples']}"
    return (
        f"Daily report {report['day_dhaka']} · {report['badge']}\n"
        f"Decisions: {report['decisions']}\n"
        f"Top vetoes: {report['top_vetoes']}\n"
        f"Fills: {report['fills']} · notional ${report['fill_notional_usd']} · fees ${report['fees_usd']}\n"
        f"Realized PnL: ${report['realized_pnl_usd']} · open positions: {report['open_positions']}\n"
        f"Exposure: {report['open_exposure_usd_by_mode']}\n"
        f"Rules: {report['rules']}\n"
        f"Calibration: {calibration_text}"
    )
