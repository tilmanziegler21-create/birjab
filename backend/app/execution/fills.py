"""Post a simulated book fill into the ledger. This is not an exchange fill."""

from __future__ import annotations

from decimal import Decimal

from app.accounting.ledger import Ledger
from app.accounting.lots import Lot
from app.market.orderbook import BookFill


def post_buy(
    ledger: Ledger,
    *,
    fill_id: str,
    reservation_id: str,
    lot_id: str,
    symbol: str,
    fill: BookFill,
) -> Lot:
    return ledger.apply_buy_fill(
        fill_id=fill_id,
        reservation_id=reservation_id,
        lot_id=lot_id,
        symbol=symbol,
        price=fill.price,
        quantity=fill.quantity,
        fee_amount=fill.fee_amount,
        fee_currency=fill.fee_currency,
    )


def post_sell(
    ledger: Ledger,
    *,
    fill_id: str,
    lot_id: str,
    hold_id: str,
    fill: BookFill,
) -> Decimal:
    return ledger.apply_sell_fill(
        fill_id=fill_id,
        lot_id=lot_id,
        price=fill.price,
        quantity=fill.quantity,
        fee_amount=fill.fee_amount,
        fee_currency=fill.fee_currency,
        hold_id=hold_id,
    )
