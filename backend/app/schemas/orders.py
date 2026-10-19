"""Order intent and state machine. Intents are not exchange commands."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.money import money


class OrderState(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PREPARED: frozenset({OrderState.SUBMITTED, OrderState.CANCELED, OrderState.REJECTED}),
    OrderState.SUBMITTED: frozenset(
        {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.UNKNOWN}
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.OPEN,
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.OPEN: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED, OrderState.UNKNOWN}
    ),
    OrderState.PARTIAL: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED, OrderState.UNKNOWN}
    ),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.OPEN,
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


class InvalidOrderTransition(ValueError):
    pass


def transition(current: OrderState, nxt: OrderState) -> OrderState:
    if nxt not in ALLOWED_TRANSITIONS[current]:
        raise InvalidOrderTransition(f"{current.value} -> {nxt.value} is not allowed")
    return nxt


class OrderIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(min_length=1, max_length=64)
    symbol: str
    side: Literal["buy", "sell"]
    purpose: Literal["entry", "exit", "emergency_exit"]
    quantity: Decimal | None = None
    quote_value: Decimal | None = None
    limit_price: Decimal | None = None
    lot_id: str | None = None
    reason: str = Field(min_length=1, max_length=500)
    source_event_ids: list[str] = Field(default_factory=list)
    strategy_id: Literal["baseline_grid", "ai_filtered_grid", "emergency"]

    @field_validator("quantity", "quote_value", "limit_price", mode="before")
    @classmethod
    def _money(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return money(value)

    @field_validator("client_order_id")
    @classmethod
    def _id(cls, value: str) -> str:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:")
        if any(char not in allowed for char in value):
            raise ValueError("client_order_id has unsupported characters")
        return value

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("symbol must be BASE/QUOTE")
        return f"{parts[0].upper()}/{parts[1].upper()}"

    @model_validator(mode="after")
    def _size(self) -> OrderIntent:
        if self.side == "buy":
            if self.purpose != "entry":
                raise ValueError("buys are entries")
            if self.quote_value is None or self.quote_value <= 0:
                raise ValueError("buy intent requires a positive quote_value")
        else:
            if self.purpose == "entry":
                raise ValueError("sells are exits")
            if self.quantity is None or self.quantity <= 0:
                raise ValueError("sell intent requires a positive quantity")
            if not self.lot_id:
                raise ValueError("sell intent requires a lot_id")
        if self.purpose == "emergency_exit" and self.strategy_id != "emergency":
            raise ValueError("emergency exits must use the emergency strategy id")
        return self

