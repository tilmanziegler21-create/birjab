"""Spot fee arithmetic. Percent inputs use the config convention: 0.10 means 0.10%."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.money import money, require_non_negative, require_positive


class FeeError(ValueError):
    pass


@dataclass(frozen=True)
class BuyEconomics:
    quote_debit: Decimal
    sellable_base: Decimal


@dataclass(frozen=True)
class SellEconomics:
    base_debit: Decimal
    quote_credit: Decimal


def buy_economics(
    *,
    price: Decimal,
    quantity: Decimal,
    fee_amount: Decimal,
    fee_currency: str,
    base_asset: str,
    quote_asset: str,
) -> BuyEconomics:
    """Fee in base reduces the base that can be sold. Fee in quote increases cash spent."""

    px = require_positive(price, "price")
    qty = require_positive(quantity, "quantity")
    fee = require_non_negative(fee_amount, "fee_amount")
    notional = px * qty
    if fee_currency == quote_asset:
        return BuyEconomics(quote_debit=notional + fee, sellable_base=qty)
    if fee_currency == base_asset:
        if fee >= qty:
            raise FeeError("base fee consumes the bought quantity")
        return BuyEconomics(quote_debit=notional, sellable_base=qty - fee)
    raise FeeError("fee currency must be the base or quote asset of the fill")


def sell_economics(
    *,
    price: Decimal,
    quantity: Decimal,
    fee_amount: Decimal,
    fee_currency: str,
    base_asset: str,
    quote_asset: str,
) -> SellEconomics:
    """`quantity` is the base removed from the lot, including a base-denominated fee."""

    px = require_positive(price, "price")
    qty = require_positive(quantity, "quantity")
    fee = require_non_negative(fee_amount, "fee_amount")
    if fee_currency == quote_asset:
        notional = px * qty
        if fee > notional:
            raise FeeError("quote fee exceeds sale proceeds")
        return SellEconomics(base_debit=qty, quote_credit=notional - fee)
    if fee_currency == base_asset:
        if fee >= qty:
            raise FeeError("base fee consumes the sold quantity")
        return SellEconomics(base_debit=qty, quote_credit=px * (qty - fee))
    raise FeeError("fee currency must be the base or quote asset of the fill")


def round_trip_net_pct(
    *,
    step_pct: Decimal,
    entry_fee_pct: Decimal,
    exit_fee_pct: Decimal,
    slippage_pct: Decimal,
) -> Decimal:
    """Percent points left after one entry fee, one exit fee, and the configured slippage."""

    return money(step_pct) - money(entry_fee_pct) - money(exit_fee_pct) - money(slippage_pct)


def meets_net_target(*, expected_net_pct: Decimal, net_target_pct: Decimal) -> bool:
    return money(expected_net_pct) >= money(net_target_pct)


def exit_limit_price(
    *,
    entry_price: Decimal,
    entry_fee_pct: Decimal,
    exit_fee_pct: Decimal,
    slippage_pct: Decimal,
    net_target_pct: Decimal,
) -> Decimal:
    """Minimum sell price so a maker-style exit still clears fees, slippage, and the net target."""

    price = require_positive(entry_price, "entry_price")
    cost = price * (Decimal("1") + money(entry_fee_pct) / Decimal("100"))
    gross_target = cost * (Decimal("1") + money(net_target_pct) / Decimal("100"))
    haircut = (Decimal("1") - money(slippage_pct) / Decimal("100")) * (
        Decimal("1") - money(exit_fee_pct) / Decimal("100")
    )
    if haircut <= 0:
        raise FeeError("exit haircut is not positive")
    return gross_target / haircut
