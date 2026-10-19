"""Illustrative fixtures only. This source does not open a connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RawItem:
    source: str
    url: str
    headline: str
    summary: str = ""
    published_at: datetime | None = None
    analysis: dict[str, object] | None = None


class MockNewsSource:
    """Returns the list it was given. Headlines are fixtures, not market events."""

    illustrative = True

    def __init__(self, items: list[RawItem] | None = None) -> None:
        self.items = list(items or [])
        self.fetches = 0

    def fetch(self, now: datetime) -> list[RawItem]:
        del now
        self.fetches += 1
        return list(self.items)
