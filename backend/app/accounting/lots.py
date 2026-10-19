"""Individual spot lots. A sell can close only the base still held by that lot."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Reservation:
    reservation_id: str
    symbol: str
    side: str
    amount: Decimal
    remaining: Decimal
    status: str


@dataclass
class BaseHold:
    hold_id: str
    lot_id: str
    quantity: Decimal
    status: str
    original_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if self.original_quantity is None:
            self.original_quantity = self.quantity


@dataclass
class Lot:
    lot_id: str
    symbol: str
    base_asset: str
    quote_asset: str
    entry_price: Decimal
    gross_base_qty: Decimal
    sellable_base_qty: Decimal
    remaining_base_qty: Decimal
    reserved_base_qty: Decimal
    quote_cost_remaining: Decimal
    entry_fee_amount: Decimal
    entry_fee_currency: str
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def available_base_qty(self) -> Decimal:
        return self.remaining_base_qty - self.reserved_base_qty

    @property
    def status(self) -> str:
        if self.remaining_base_qty == 0:
            return "closed"
        if self.remaining_base_qty < self.sellable_base_qty:
            return "partial"
        return "open"


def split_symbol(symbol: str) -> tuple[str, str]:
    base, quote = symbol.split("/")
    return base, quote
