"""Short-lived public klines for Δ and display-only regime. GET only, no keys."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.market.public import PublicBookClient, PublicBookError
from app.market.regime import for_symbol


class KlineCache:
    def __init__(self, client: PublicBookClient, *, ttl_sec: int = 60, store_path: Path | None = None) -> None:
        self.client = client
        self.ttl = timedelta(seconds=ttl_sec)
        self.store_path = store_path
        self.changes: dict[str, dict[str, Any]] = {}
        self.regimes: dict[str, dict[str, Any]] = {}
        self.fetched_at: datetime | None = None

    def maybe_refresh(self, symbols: list[str], quote: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self.fetched_at is not None and now - self.fetched_at < self.ttl:
            return
        for symbol in symbols:
            base = symbol.split("/")[0]
            pair = symbol if "/" in symbol else f"{base}/{quote}"
            try:
                hour = self.client.fetch_klines(base, quote, interval="1h", limit=24)
                minute = self.client.fetch_klines(base, quote, interval="1m", limit=6)
            except PublicBookError:
                hour, minute = [], []
            last = hour[-1].close if hour else None
            if last is not None and last > 0:
                def pct(then: Decimal | None) -> Decimal | None:
                    if then is None or then <= 0:
                        return None
                    return (last - then) / then * Decimal("100")

                m5 = minute[0].close if len(minute) >= 6 else None
                h1 = hour[-2].close if len(hour) >= 2 else None
                d1 = hour[0].close if len(hour) >= 24 else None
                self.changes[pair] = {"m5": pct(m5), "h1": pct(h1), "d1": pct(d1)}
            classified = for_symbol(pair, client=self.client, store_path=self.store_path)
            self.regimes[base] = classified
            self.regimes[pair] = classified
        self.fetched_at = now
