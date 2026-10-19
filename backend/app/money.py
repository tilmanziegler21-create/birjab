"""Decimal money helpers. Floats are rejected at the boundary."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum


class QuoteCurrency(str, Enum):
    USDC = "USDC"
    USDT = "USDT"
    EUR = "EUR"


class MoneyError(ValueError):
    pass


def money(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError("money must be Decimal, int, or str; float is forbidden")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise MoneyError("invalid decimal string") from exc
    else:
        raise MoneyError(f"unsupported money type: {type(value).__name__}")
    if not result.is_finite():
        raise MoneyError("non-finite money")
    return result


def percent_of(amount: Decimal, pct: Decimal) -> Decimal:
    return money(amount) * money(pct) / Decimal("100")


def require_positive(value: Decimal, name: str) -> Decimal:
    amount = money(value)
    if amount <= 0:
        raise MoneyError(f"{name} must be positive")
    return amount


def require_non_negative(value: Decimal, name: str) -> Decimal:
    amount = money(value)
    if amount < 0:
        raise MoneyError(f"{name} must be non-negative")
    return amount
