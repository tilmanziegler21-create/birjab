"""Exchange port. Implementations in this phase must not transmit live orders."""

from __future__ import annotations

from typing import Protocol

from app.schemas.orders import OrderIntent


class ExchangePort(Protocol):
    def place_order(self, intent: OrderIntent) -> None:
        """Accept an intent. Phase 1 implementations do not fill or transmit it."""


class CancelPort(Protocol):
    def cancel_order(self, client_order_id: str) -> None:
        """Request a cancel. The live implementation refuses."""
