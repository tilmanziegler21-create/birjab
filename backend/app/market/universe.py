"""Hard filters for the spot universe. No network calls."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.money import QuoteCurrency, money

START_UNIVERSE = frozenset({"BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "ADA", "AVAX"})


class MarketFilterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_asset: str
    quote_currency: QuoteCurrency
    market_status: Literal["trading", "halt", "break", "post_only"]
    bid: Decimal
    ask: Decimal
    order_quote: Decimal
    order_qty: Decimal
    order_price: Decimal
    ask_depth_quote: Decimal
    bid_depth_quote: Decimal
    min_notional: Decimal
    tick_size: Decimal
    lot_size: Decimal
    is_recent_listing: bool = False
    atr_pct: Decimal | None = None

    @field_validator(
        "bid",
        "ask",
        "order_quote",
        "order_qty",
        "order_price",
        "ask_depth_quote",
        "bid_depth_quote",
        "min_notional",
        "tick_size",
        "lot_size",
        "atr_pct",
        mode="before",
    )
    @classmethod
    def _money(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return money(value)


class UniversePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_currency: QuoteCurrency
    watchlist: list[str]
    min_depth_multiplier: Decimal
    max_spread_pct: Decimal | None = None
    max_atr_pct: Decimal | None = None
    allow_extended_universe: bool = False

    @field_validator("min_depth_multiplier", "max_spread_pct", "max_atr_pct", mode="before")
    @classmethod
    def _money(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return money(value)


class UniverseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reasons: list[str]


def hard_filter(snapshot: MarketFilterInput, policy: UniversePolicy) -> UniverseDecision:
    reasons: list[str] = []
    if snapshot.base_asset not in policy.watchlist and not policy.allow_extended_universe:
        reasons.append("asset_not_in_universe")
    if snapshot.quote_currency != policy.quote_currency:
        reasons.append("quote_mismatch")
    if snapshot.market_status != "trading":
        reasons.append("market_not_trading")
    if snapshot.is_recent_listing:
        reasons.append("recent_listing")
    if snapshot.bid <= 0 or snapshot.ask < snapshot.bid:
        reasons.append("crossed_or_invalid_book")
    elif policy.max_spread_pct is not None:
        mid = (snapshot.ask + snapshot.bid) / Decimal("2")
        spread_pct = (snapshot.ask - snapshot.bid) / mid * Decimal("100")
        if spread_pct > policy.max_spread_pct:
            reasons.append("spread_too_wide")
    required_depth = snapshot.order_quote * policy.min_depth_multiplier
    if snapshot.ask_depth_quote < required_depth or snapshot.bid_depth_quote < required_depth:
        reasons.append("insufficient_depth")
    if snapshot.order_quote < snapshot.min_notional:
        reasons.append("below_min_notional")
    if not _aligned(snapshot.order_qty, snapshot.lot_size):
        reasons.append("lot_size_mismatch")
    if not _aligned(snapshot.order_price, snapshot.tick_size):
        reasons.append("tick_size_mismatch")
    if (
        policy.max_atr_pct is not None
        and snapshot.atr_pct is not None
        and snapshot.atr_pct > policy.max_atr_pct
    ):
        reasons.append("abnormal_volatility")
    return UniverseDecision(allowed=not reasons, reasons=reasons)


def _aligned(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return False
    units = value / step
    return units == units.to_integral_value()
