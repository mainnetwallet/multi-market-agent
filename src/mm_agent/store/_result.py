"""Small typing helpers for SQLAlchemy results."""

from __future__ import annotations

from typing import Any


def rowcount(result: Any) -> int:
    """SQLAlchemy types `Connection.execute` as Result; DML returns CursorResult."""

    return int(getattr(result, "rowcount", 0) or 0)
