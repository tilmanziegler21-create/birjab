"""Exact decimal column. SQLite stores text so tests do not pass through float."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator

from app.money import money


class MoneyAmount(TypeDecorator[Decimal]):
    impl = String(80)
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(38, 18, asdecimal=True))
        return dialect.type_descriptor(String(80))

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        amount = money(value)
        if dialect.name == "postgresql":
            return amount
        return format(amount, "f")

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        return money(str(value))
