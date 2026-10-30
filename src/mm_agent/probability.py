"""Fair-probability construction with a capped, sourced LLM adjustment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain import ResearchReport, utcnow

PP = Decimal("0.01")


@dataclass(frozen=True)
class FairProbability:
    p_mid: Decimal
    p_fair: Decimal
    adjustment: Decimal
    used_research: bool
    research_reasons: tuple[str, ...]
    research_id: str | None = None


def compute_fair_probability(
    mid: Decimal,
    report: ResearchReport | None,
    *,
    min_sources: int,
    max_age_minutes: int,
    max_adjustment_pp: Decimal,
    now: datetime | None = None,
    research_id: str | None = None,
) -> FairProbability:
    if report is None:
        return FairProbability(
            p_mid=mid,
            p_fair=mid,
            adjustment=Decimal("0"),
            used_research=False,
            research_reasons=("NO_RESEARCH",),
        )
    if not report.is_usable(min_sources, max_age_minutes, now or utcnow()):
        return FairProbability(
            p_mid=mid,
            p_fair=mid,
            adjustment=Decimal("0"),
            used_research=False,
            research_reasons=("RESEARCH_STALE_OR_UNDERSOURCED",),
            research_id=research_id,
        )
    cap = max_adjustment_pp * PP
    raw_adjustment = report.probability - mid
    adjustment = max(-cap, min(cap, raw_adjustment))
    p_fair = max(Decimal("0.01"), min(Decimal("0.99"), mid + adjustment))
    return FairProbability(
        p_mid=mid,
        p_fair=p_fair,
        adjustment=adjustment,
        used_research=True,
        research_reasons=(),
        research_id=research_id,
    )
