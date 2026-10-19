"""Official venue status arrives as a snapshot. Nothing here contacts a venue."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExchangeStatusSnapshot:
    venue: str
    status: str
    affected_assets: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.venue):
            raise ValueError("invalid venue")
        if self.observed_at.tzinfo is None:
            raise ValueError("naive_timestamp")
        object.__setattr__(self, "status", self.status.strip().lower())
        object.__setattr__(
            self,
            "affected_assets",
            tuple(item.strip().upper() for item in self.affected_assets if item.strip()),
        )
