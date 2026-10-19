"""Pure exposure and drawdown limits."""

from __future__ import annotations

from decimal import Decimal

from app.accounting.pnl import drawdown_pct
from app.money import money


def exposure_pct(*, value: Decimal, equity: Decimal) -> Decimal:
    equity_value = money(equity)
    if equity_value <= 0:
        raise ValueError("equity must be positive")
    return money(value) / equity_value * Decimal("100")


def exceeds_cap(*, value: Decimal, equity: Decimal, cap_pct: Decimal) -> bool:
    return exposure_pct(value=value, equity=equity) > money(cap_pct)


def drawdown_exceeds(*, peak: Decimal, current: Decimal, cap_pct: Decimal) -> bool:
    return drawdown_pct(peak=peak, current=current) > money(cap_pct)
