"""Deterministic risk outputs. These are not exchange orders."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.money import money


class RiskRegime(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    HIGH_RISK = "HIGH_RISK"
    PAUSE_BUYS = "PAUSE_BUYS"
    HALT_ALL = "HALT_ALL"
    DEPEG = "DEPEG"
    EXCHANGE_ISSUE = "EXCHANGE_ISSUE"


class RiskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    client_order_id: str | None = None
    reservation_id: str | None = None
    reserved_quote: Decimal | None = None
    regime: RiskRegime = RiskRegime.NORMAL

    def model_post_init(self, __context: object) -> None:
        if self.reserved_quote is not None:
            object.__setattr__(self, "reserved_quote", money(self.reserved_quote))
        if self.allowed and self.reason == "":
            raise ValueError("allowed decisions need a reason")
