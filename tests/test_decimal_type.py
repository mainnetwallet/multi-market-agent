"""Money columns are text on SQLite and NUMERIC on PostgreSQL."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.dialects import postgresql, sqlite

from mm_agent.store.models import DecimalString


def test_decimal_string_column_type_per_dialect() -> None:
    column_type = DecimalString(40)
    assert column_type.compile(dialect=sqlite.dialect()) == "VARCHAR(40)"
    assert column_type.compile(dialect=postgresql.dialect()) == "NUMERIC"


def test_decimal_string_sqlite_round_trip_is_exact() -> None:
    column_type = DecimalString(40)
    dialect = sqlite.dialect()
    stored = column_type.process_bind_param(Decimal("0.57000"), dialect)
    assert stored == "0.57000"
    assert column_type.process_result_value(stored, dialect) == Decimal("0.57000")


def test_decimal_string_postgres_passes_decimals_through() -> None:
    column_type = DecimalString(40)
    dialect = postgresql.dialect()
    assert column_type.process_bind_param(Decimal("0.57"), dialect) == Decimal("0.57")
    assert column_type.process_bind_param(None, dialect) is None
    assert column_type.process_result_value(Decimal("1.25"), dialect) == Decimal("1.25")
