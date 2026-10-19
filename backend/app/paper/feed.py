"""Book feeds for the paper runtime. Synthetic numbers are labelled as such."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from app.market.orderbook import Level, OrderBook
from app.market.public import (
    PublicBookClient,
    PublicBookError,
    SpotFilters,
    assert_public_read_url,
    parse_spot_filters,
)
from app.money import money


class FeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedSnapshot:
    book: OrderBook
    filters: SpotFilters
    received_at: datetime
    latency_ms: int


SYNTHETIC_FILTERS_NOTE = "synthetic market parameters, not a venue"


@dataclass
class ScriptedFeed:
    """Books are set by the caller. Used by tests and stress scenarios."""

    name: str = "scripted"
    illustrative: bool = True
    books: dict[str, OrderBook] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    outage: bool = False
    quote_deviation: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.01")
    lot_size: Decimal = Decimal("0.00001")
    min_notional: Decimal = Decimal("5")

    def set_book(self, book: OrderBook, *, status: str = "trading") -> None:
        self.books[book.symbol] = book
        self.statuses[book.symbol] = status

    def fetch(self, symbol: str, now: datetime) -> FeedSnapshot:
        if self.outage:
            raise FeedError("feed_outage")
        book = self.books.get(symbol)
        if book is None:
            raise FeedError("symbol_unavailable")
        base, quote = symbol.split("/")
        filters = SpotFilters(
            base=base,
            quote=quote,
            market_status=self.statuses.get(symbol, "trading"),
            tick_size=self.tick_size,
            lot_size=self.lot_size,
            min_notional=self.min_notional,
        )
        latency = int((now - book.updated_at).total_seconds() * 1000)
        return FeedSnapshot(book=book, filters=filters, received_at=now, latency_ms=max(latency, 0))

    def quote_deviation_pct(self, now: datetime) -> Decimal | None:
        del now
        return self.quote_deviation


class SyntheticFeed:
    """Seeded random walk from 100. Not market data; for local wiring only."""

    name = "synthetic"
    illustrative = True

    def __init__(
        self,
        symbols: list[str],
        *,
        seed: int = 7,
        start_price: Decimal = Decimal("100"),
        step_pct: Decimal = Decimal("0.05"),
        spread_pct: Decimal = Decimal("0.02"),
        level_qty: Decimal = Decimal("10"),
        levels: int = 5,
    ) -> None:
        self._random = random.Random(seed)
        self._mid: dict[str, Decimal] = {symbol: money(start_price) for symbol in symbols}
        self.step_pct = money(step_pct)
        self.spread_pct = money(spread_pct)
        self.level_qty = money(level_qty)
        self.levels = levels
        self.outage = False
        self.statuses: dict[str, str] = {symbol: "trading" for symbol in symbols}
        self.quote_deviation = Decimal("0")
        self.tick_size = Decimal("0.01")
        self.lot_size = Decimal("0.00001")
        self.min_notional = Decimal("5")
        self.shocks: dict[str, Decimal] = {}

    def shock(self, symbol: str, pct: Decimal) -> None:
        self.shocks[symbol] = money(pct)

    def step(self) -> None:
        for symbol, mid in list(self._mid.items()):
            shock = self.shocks.pop(symbol, None)
            if shock is not None:
                move = shock / Decimal("100")
            else:
                sample = Decimal(str(self._random.gauss(0, 1))).quantize(Decimal("0.0001"))
                move = sample * self.step_pct / Decimal("100")
            self._mid[symbol] = max(mid * (Decimal("1") + move), self.tick_size * 10)

    def fetch(self, symbol: str, now: datetime) -> FeedSnapshot:
        if self.outage:
            raise FeedError("feed_outage")
        mid = self._mid.get(symbol)
        if mid is None:
            raise FeedError("symbol_unavailable")
        half = mid * self.spread_pct / Decimal("200")
        bid = _quantize(mid - half, self.tick_size)
        ask = _quantize(mid + half, self.tick_size)
        if ask <= bid:
            ask = bid + self.tick_size
        bids = [Level(bid - self.tick_size * i, self.level_qty) for i in range(self.levels)]
        asks = [Level(ask + self.tick_size * i, self.level_qty) for i in range(self.levels)]
        book = OrderBook(symbol=symbol, bids=bids, asks=asks, updated_at=now)
        base, quote = symbol.split("/")
        filters = SpotFilters(
            base=base,
            quote=quote,
            market_status=self.statuses.get(symbol, "trading"),
            tick_size=self.tick_size,
            lot_size=self.lot_size,
            min_notional=self.min_notional,
        )
        return FeedSnapshot(book=book, filters=filters, received_at=now, latency_ms=0)

    def quote_deviation_pct(self, now: datetime) -> Decimal | None:
        del now
        return self.quote_deviation


class PublicFeed:
    """Read-only public books. Status comes from exchangeInfo, refreshed every few minutes."""

    name = "binance_public"
    illustrative = False

    def __init__(
        self,
        client: PublicBookClient,
        *,
        depth_limit: int = 20,
        info_refresh_sec: int = 300,
        raw_get: Callable[[str], bytes] | None = None,
    ) -> None:
        self.client = client
        self.depth_limit = depth_limit
        self.info_refresh_sec = info_refresh_sec
        self._raw_get = raw_get
        self._filters: dict[str, tuple[SpotFilters, datetime]] = {}
        self._peg: tuple[Decimal, datetime] | None = None
        self._last_quote = "USDC"
        self._book_ids: dict[str, tuple[int, datetime]] = {}
        self.last_error: str | None = None

    def fetch(self, symbol: str, now: datetime) -> FeedSnapshot:
        base, quote = symbol.split("/")
        try:
            filters = self._spot_filters(base, quote, now)
            book = self.client.fetch_depth(base, quote, now, limit=self.depth_limit)
        except PublicBookError as exc:
            self.last_error = str(exc)
            raise FeedError(str(exc)) from exc
        self.last_error = None
        # REST depth carries no timestamp. A book whose lastUpdateId has not moved since it was
        # first seen keeps that first-seen time, so the stale rule can catch a frozen feed.
        if book.update_id is not None:
            previous = self._book_ids.get(symbol)
            if previous is not None and previous[0] == book.update_id:
                book.updated_at = previous[1]
            else:
                self._book_ids[symbol] = (book.update_id, now)
        return FeedSnapshot(book=book, filters=filters, received_at=now, latency_ms=0)

    def quote_deviation_pct(self, now: datetime) -> Decimal | None:
        """USDC/USDT cross as a peg proxy. EUR has no peg and reports 0. Unknown -> None."""

        quote = self._last_quote
        if quote == "EUR":
            return Decimal("0")
        if quote not in {"USDC", "USDT"}:
            return None
        if self._peg is not None and (now - self._peg[1]).total_seconds() < self.info_refresh_sec:
            return self._peg[0]
        try:
            book = self.client.fetch_book_ticker("USDC", "USDT", now)
        except PublicBookError:
            return None
        mid = (book.best_bid() + book.best_ask()) / Decimal("2")
        deviation = abs(mid - Decimal("1")) * Decimal("100")
        self._peg = (deviation, now)
        return deviation

    def _spot_filters(self, base: str, quote: str, now: datetime) -> SpotFilters:
        self._last_quote = quote.upper()
        key = f"{base}/{quote}"
        cached = self._filters.get(key)
        if cached is not None and (now - cached[1]).total_seconds() < self.info_refresh_sec:
            return cached[0]
        getter = self._raw_get or self.client._transport
        url = f"https://api.binance.com/api/v3/exchangeInfo?symbol={base.upper()}{quote.upper()}"
        assert_public_read_url(url)
        try:
            payload = json.loads(getter(url))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PublicBookError("invalid_json") from exc
        filters = parse_spot_filters(payload, base, quote)
        self._filters[key] = (filters, now)
        return filters


def _quantize(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def age_seconds(now: datetime, then: datetime) -> int:
    delta: timedelta = now - then
    return delta.days * 86400 + delta.seconds
