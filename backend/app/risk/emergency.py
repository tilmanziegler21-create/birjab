"""Manual pause and confirmed liquidation intents. This module sends no orders."""

from __future__ import annotations

from app.accounting.lots import Lot
from app.schemas.orders import OrderIntent

CONFIRM_LIQUIDATE = "CONFIRM_LIQUIDATE"


class EmergencyControl:
    def __init__(self) -> None:
        self.pause_new_buys = False
        self.halt_all = False
        self.liquidate_confirmed = False

    def pause(self) -> None:
        self.pause_new_buys = True

    def request_liquidate(self, confirmation: str, lots: list[Lot]) -> list[OrderIntent]:
        if confirmation != CONFIRM_LIQUIDATE:
            return []
        self.pause_new_buys = True
        self.halt_all = True
        self.liquidate_confirmed = True
        intents: list[OrderIntent] = []
        for lot in lots:
            quantity = lot.available_base_qty
            if quantity <= 0:
                continue
            intents.append(
                OrderIntent(
                    client_order_id=f"liq-{lot.lot_id}",
                    symbol=lot.symbol,
                    side="sell",
                    purpose="emergency_exit",
                    quantity=quantity,
                    lot_id=lot.lot_id,
                    reason="confirmed emergency liquidation",
                    strategy_id="emergency",
                )
            )
        return intents
