"""Liquid equity. Open lots are marked on the bid, then estimated exit fees are removed."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.money import money, percent_of, require_non_negative


class MissingMark(ValueError):
    pass


@dataclass(frozen=True)
class OpenLotMark:
    symbol: str
    remaining_base_qty: Decimal
    quote_cost_remaining: Decimal
    bid: Decimal


@dataclass
class EquityBreakdown:
    free_quote: Decimal
    reserved_quote: Decimal
    gross_position_value: Decimal
    estimated_exit_fees: Decimal
    net_position_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    profit_reserve: Decimal = Decimal("0")
    fees_by_currency: dict[str, Decimal] = field(default_factory=dict)

    @property
    def cash(self) -> Decimal:
        return self.free_quote + self.reserved_quote + self.profit_reserve


def liquid_equity(
    *,
    free_quote: Decimal,
    reserved_quote: Decimal,
    lots: list[OpenLotMark],
    profit_reserve: Decimal = Decimal("0"),
    exit_fee_pct: Decimal,
    realized_pnl: Decimal,
    fees_by_currency: dict[str, Decimal],
) -> EquityBreakdown:
    free = money(free_quote)
    reserved = money(reserved_quote)
    fee_pct = money(exit_fee_pct)
    if fee_pct < 0:
        raise ValueError("exit fee percent must be non-negative")
    gross = Decimal("0")
    exit_fees = Decimal("0")
    unrealized = Decimal("0")
    for lot in lots:
        qty = require_non_negative(lot.remaining_base_qty, "remaining_base_qty")
        if qty == 0:
            continue
        bid = money(lot.bid)
        if bid <= 0:
            raise MissingMark(lot.symbol)
        position_gross = bid * qty
        position_fee = percent_of(position_gross, fee_pct)
        net = position_gross - position_fee
        gross += position_gross
        exit_fees += position_fee
        unrealized += net - money(lot.quote_cost_remaining)
    net_position = gross - exit_fees
    reserved_profit = money(profit_reserve)
    equity = free + reserved + reserved_profit + net_position
    return EquityBreakdown(
        free_quote=free,
        reserved_quote=reserved,
        profit_reserve=reserved_profit,
        gross_position_value=gross,
        estimated_exit_fees=exit_fees,
        net_position_value=net_position,
        equity=equity,
        realized_pnl=money(realized_pnl),
        unrealized_pnl=unrealized,
        fees_by_currency={key: money(value) for key, value in fees_by_currency.items()},
    )


def drawdown_pct(*, peak: Decimal, current: Decimal) -> Decimal:
    peak_value = money(peak)
    current_value = money(current)
    if peak_value <= 0:
        raise ValueError("peak equity must be positive")
    if current_value >= peak_value:
        return Decimal("0")
    return (peak_value - current_value) / peak_value * Decimal("100")
