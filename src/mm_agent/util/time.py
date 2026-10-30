"""Time helpers. Storage is UTC; user-facing output is Asia/Dhaka."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DHAKA = ZoneInfo("Asia/Dhaka")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_dhaka() -> datetime:
    return datetime.now(DHAKA)


def fmt_dhaka(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M:%S +06")


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
