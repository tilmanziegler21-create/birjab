"""Order state transitions. UNKNOWN is reconciled; it is not resubmitted."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.schemas.orders import (
    ALLOWED_TRANSITIONS,
    InvalidOrderTransition,
    OrderIntent,
    OrderState,
    transition,
)

@dataclass
class PaperOrder:
    client_order_id: str
    intent: OrderIntent
    state: OrderState
    limit_price: Decimal
    quote_budget: Decimal
    base_budget: Decimal
    filled_quote: Decimal = Decimal("0")
    filled_base: Decimal = Decimal("0")
    fill_ids: list[str] = field(default_factory=list)

    @property
    def remaining_quote(self) -> Decimal:
        return self.quote_budget - self.filled_quote

    @property
    def remaining_base(self) -> Decimal:
        return self.base_budget - self.filled_base


def advance(order: PaperOrder, nxt: OrderState) -> OrderState:
    order.state = transition(order.state, nxt)
    return order.state


__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidOrderTransition",
    "OrderState",
    "PaperOrder",
    "advance",
    "transition",
]
