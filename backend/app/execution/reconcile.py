"""Map UNKNOWN back to a known local state. This does not submit a new order."""

from __future__ import annotations

from app.execution.orders import PaperOrder, advance
from app.schemas.orders import OrderState


def reconcile(order: PaperOrder) -> PaperOrder:
    if order.state != OrderState.UNKNOWN:
        return order
    if order.intent.side == "buy":
        if order.filled_quote == 0:
            nxt = OrderState.OPEN
        elif order.remaining_quote <= 0:
            nxt = OrderState.FILLED
        else:
            nxt = OrderState.PARTIAL
    elif order.filled_base == 0:
        nxt = OrderState.OPEN
    elif order.remaining_base <= 0:
        nxt = OrderState.FILLED
    else:
        nxt = OrderState.PARTIAL
    advance(order, nxt)
    if order.state == OrderState.SUBMITTED:
        raise RuntimeError("reconcile must not resubmit")
    return order
