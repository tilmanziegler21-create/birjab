"""Simulated spot book. Fills cannot exceed displayed depth. No network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from app.money import money, percent_of, require_non_negative, require_positive


@dataclass
class Level:
    price: Decimal
    base_qty: Decimal


@dataclass(frozen=True)
class BookFill:
    price: Decimal
    quantity: Decimal
    fee_amount: Decimal
    fee_currency: str
    quote_amount: Decimal


@dataclass
class OrderBook:
    symbol: str
    bids: list[Level]
    asks: list[Level]
    updated_at: datetime
    update_id: int | None = None

    def __post_init__(self) -> None:
        base, quote = self.symbol.split("/")
        self.symbol = f"{base.upper()}/{quote.upper()}"
        self.bids = sorted(
            (Level(money(level.price), money(level.base_qty)) for level in self.bids),
            key=lambda level: level.price,
            reverse=True,
        )
        self.asks = sorted(
            (Level(money(level.price), money(level.base_qty)) for level in self.asks),
            key=lambda level: level.price,
        )

    def best_bid(self) -> Decimal:
        if not self.bids or self.bids[0].price <= 0:
            raise ValueError("book has no bid")
        return self.bids[0].price

    def best_ask(self) -> Decimal:
        if not self.asks or self.asks[0].price <= 0:
            raise ValueError("book has no ask")
        return self.asks[0].price

    def spread_pct(self) -> Decimal:
        bid = self.best_bid()
        ask = self.best_ask()
        if ask < bid:
            raise ValueError("crossed book")
        mid = (ask + bid) / Decimal("2")
        return (ask - bid) / mid * Decimal("100")

    def bid_depth_quote(self) -> Decimal:
        return sum((level.price * level.base_qty for level in self.bids), Decimal("0"))

    def ask_depth_quote(self) -> Decimal:
        return sum((level.price * level.base_qty for level in self.asks), Decimal("0"))


QTY_STEP = Decimal("0.00000001")


def _down(quantity: Decimal) -> Decimal:
    """Fill quantities are finite decimals, as on a venue. Rounding is toward zero."""

    return quantity.quantize(QTY_STEP, rounding=ROUND_DOWN)


def match_buy(
    asks: list[Level],
    *,
    limit_price: Decimal,
    quote_budget: Decimal,
    fee_pct: Decimal,
    slippage_pct: Decimal,
    fee_currency: str,
) -> list[BookFill]:
    """Walk asks. A level is skipped when slippage would push the price through the limit."""

    limit = require_positive(limit_price, "limit_price")
    budget = require_positive(quote_budget, "quote_budget")
    fee_rate = require_non_negative(fee_pct, "fee_pct") / Decimal("100")
    slip = require_non_negative(slippage_pct, "slippage_pct") / Decimal("100")
    fills: list[BookFill] = []
    for level in asks:
        if budget <= 0 or level.base_qty <= 0:
            break
        if level.price > limit:
            break
        price = level.price * (Decimal("1") + slip)
        if price > limit:
            break
        unit = price * (Decimal("1") + fee_rate)
        if unit <= 0:
            break
        quantity = min(level.base_qty, _down(budget / unit))
        if quantity <= 0:
            break
        notional = quantity * price
        fee = notional * fee_rate
        debit = notional + fee
        if debit > budget:
            quantity = _down(budget / unit)
            if quantity <= 0:
                break
            notional = quantity * price
            fee = notional * fee_rate
            debit = notional + fee
        level.base_qty -= quantity
        budget -= debit
        fills.append(
            BookFill(
                price=price,
                quantity=quantity,
                fee_amount=fee,
                fee_currency=fee_currency,
                quote_amount=debit,
            )
        )
    return fills


def match_sell(
    bids: list[Level],
    *,
    limit_price: Decimal,
    base_qty: Decimal,
    fee_pct: Decimal,
    slippage_pct: Decimal,
    fee_currency: str,
) -> list[BookFill]:
    """Walk bids. Fill price is worse by the slippage assumption and must stay at or above the limit."""

    limit = require_positive(limit_price, "limit_price")
    remaining = require_positive(base_qty, "base_qty")
    fee_rate = require_non_negative(fee_pct, "fee_pct") / Decimal("100")
    slip = require_non_negative(slippage_pct, "slippage_pct") / Decimal("100")
    fills: list[BookFill] = []
    for level in bids:
        if remaining <= 0 or level.base_qty <= 0:
            break
        price = level.price * (Decimal("1") - slip)
        if price < limit or level.price < limit:
            break
        quantity = min(level.base_qty, remaining)
        notional = quantity * price
        fee = percent_of(notional, money(fee_pct))
        credit = notional - fee
        if credit < 0:
            break
        level.base_qty -= quantity
        remaining -= quantity
        fills.append(
            BookFill(
                price=price,
                quantity=quantity,
                fee_amount=fee,
                fee_currency=fee_currency,
                quote_amount=credit,
            )
        )
    return fills
