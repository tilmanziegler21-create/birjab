"""Public read-only books. This module does not send orders or read API keys."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.market.orderbook import Level, OrderBook
from app.money import MoneyError, money

ALLOWED_HOSTS = frozenset({"api.binance.com"})
ALLOWED_PATHS = frozenset(
    {
        "/api/v3/depth",
        "/api/v3/ticker/bookTicker",
        "/api/v3/trades",
        "/api/v3/klines",
        "/api/v3/exchangeInfo",
    }
)
_DEPTH_LIMITS = frozenset({5, 10, 20, 50, 100, 500, 1000, 5000})
_SIGNED_QUERY = frozenset({"apikey", "signature", "secret", "timestamp", "recvwindow"})


class PublicBookError(ValueError):
    pass


@dataclass(frozen=True)
class PublicTrade:
    price: Decimal
    quantity: Decimal
    traded_at: datetime
    buyer_is_maker: bool


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    open_price: Decimal | None = None
    interval: str | None = None


@dataclass(frozen=True)
class SpotFilters:
    base: str
    quote: str
    market_status: str
    tick_size: Decimal
    lot_size: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class TimedBook:
    book: OrderBook
    received_at: datetime
    latency_ms: int
    stale: bool


def depth_url(base: str, quote: str, *, limit: int = 20) -> str:
    _asset(base)
    _asset(quote)
    if limit not in _DEPTH_LIMITS:
        raise PublicBookError("bad_depth_limit")
    symbol = f"{base.upper()}{quote.upper()}"
    return f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit={limit}"


_KLINE_INTERVALS = frozenset({"1m", "5m", "1h", "1d"})


def klines_url(base: str, quote: str, *, interval: str = "1m", limit: int = 500) -> str:
    _asset(base)
    _asset(quote)
    if interval not in _KLINE_INTERVALS:
        raise PublicBookError("bad_kline_interval")
    if not 1 <= limit <= 1000:
        raise PublicBookError("bad_kline_limit")
    symbol = f"{base.upper()}{quote.upper()}"
    return f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"


def book_ticker_url(base: str, quote: str) -> str:
    _asset(base)
    _asset(quote)
    symbol = f"{base.upper()}{quote.upper()}"
    return f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}"


def assert_public_read_url(url: str) -> None:
    if not url.startswith("https://"):
        raise PublicBookError("https_required")
    rest = url[len("https://") :]
    host_port, sep, path_query = rest.partition("/")
    if not sep or "@" in host_port:
        raise PublicBookError("host_not_allowlisted")
    host = host_port.split(":", 1)[0].lower()
    if host not in ALLOWED_HOSTS:
        raise PublicBookError("host_not_allowlisted")
    path, _, query = path_query.partition("?")
    path = "/" + path
    if path not in ALLOWED_PATHS:
        raise PublicBookError("path_not_public_read")
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key in _SIGNED_QUERY or "order" in key:
            raise PublicBookError("signed_query_forbidden")


class PublicBookClient:
    """GET helper. The transport is injected. Nothing here places an order."""

    def __init__(self, transport: Callable[[str], bytes]) -> None:
        self._transport = transport
        self.requests: list[str] = []

    def fetch_depth(self, base: str, quote: str, now: datetime, *, limit: int = 20) -> OrderBook:
        url = depth_url(base, quote, limit=limit)
        return self._fetch_book(url, f"{base.upper()}/{quote.upper()}", now)

    def fetch_book_ticker(self, base: str, quote: str, now: datetime) -> OrderBook:
        url = book_ticker_url(base, quote)
        return self._fetch_book(url, f"{base.upper()}/{quote.upper()}", now)

    def fetch_klines(self, base: str, quote: str, *, interval: str = "1m", limit: int = 500) -> list[Candle]:
        url = klines_url(base, quote, interval=interval, limit=limit)
        assert_public_read_url(url)
        self.requests.append(url)
        raw = self._transport(url)
        if not isinstance(raw, bytes):
            raise PublicBookError("body_not_bytes")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublicBookError("invalid_json") from exc
        candles = parse_klines(payload)
        return [Candle(c.open_time, c.high, c.low, c.close, c.volume, c.open_price, interval) for c in candles]

    def _fetch_book(self, url: str, symbol: str, now: datetime) -> OrderBook:
        assert_public_read_url(url)
        self.requests.append(url)
        raw = self._transport(url)
        if not isinstance(raw, bytes):
            raise PublicBookError("body_not_bytes")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublicBookError("invalid_json") from exc
        if "/bookTicker" in url:
            return parse_book_ticker(payload, symbol, now)
        return parse_depth(payload, symbol, now)


def parse_depth(payload: object, symbol: str, updated_at: datetime) -> OrderBook:
    if not isinstance(payload, dict):
        raise PublicBookError("depth_not_object")
    bids = payload.get("bids")
    asks = payload.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise PublicBookError("depth_levels_missing")
    if not bids or not asks:
        raise PublicBookError("empty_book")
    update_id = payload.get("lastUpdateId")
    if update_id is not None and (isinstance(update_id, bool) or not isinstance(update_id, int)):
        raise PublicBookError("bad_update_id")
    book = OrderBook(
        symbol=symbol,
        bids=[_level(row) for row in bids],
        asks=[_level(row) for row in asks],
        updated_at=updated_at,
        update_id=update_id,
    )
    _require_uncrossed(book)
    return book


def parse_book_ticker(payload: object, symbol: str, updated_at: datetime) -> OrderBook:
    if not isinstance(payload, dict):
        raise PublicBookError("ticker_not_object")
    bid = payload.get("bidPrice")
    bid_qty = payload.get("bidQty")
    ask = payload.get("askPrice")
    ask_qty = payload.get("askQty")
    book = OrderBook(
        symbol=symbol,
        bids=[_level((bid, bid_qty))],
        asks=[_level((ask, ask_qty))],
        updated_at=updated_at,
    )
    _require_uncrossed(book)
    return book


def parse_book_frame(
    frame: str | bytes,
    *,
    symbol: str,
    received_at: datetime,
    stale_after_sec: int,
) -> TimedBook:
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    if not isinstance(frame, str):
        raise PublicBookError("frame_not_text")
    try:
        payload = json.loads(frame)
    except json.JSONDecodeError as exc:
        raise PublicBookError("invalid_json") from exc
    if isinstance(payload, dict) and "data" in payload and "stream" in payload:
        payload = payload["data"]
    if isinstance(payload, dict) and "bids" not in payload and ("b" in payload or "a" in payload):
        raise PublicBookError("partial_delta_not_a_book")
    if not isinstance(payload, dict):
        raise PublicBookError("depth_not_object")
    updated_at = received_at
    event_ms = payload.get("E")
    if event_ms is not None:
        if isinstance(event_ms, bool) or not isinstance(event_ms, int):
            raise PublicBookError("bad_event_time")
        updated_at = datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc)
    book = parse_depth(payload, symbol, updated_at)
    if received_at.tzinfo is None or updated_at.tzinfo is None:
        raise PublicBookError("naive_timestamp")
    latency_ms = int((received_at - updated_at).total_seconds() * 1000)
    if latency_ms < 0:
        latency_ms = 0
    stale = (received_at - updated_at).total_seconds() > stale_after_sec
    return TimedBook(book=book, received_at=received_at, latency_ms=latency_ms, stale=stale)


def parse_trades(payload: object) -> list[PublicTrade]:
    if not isinstance(payload, list):
        raise PublicBookError("trades_not_a_list")
    trades: list[PublicTrade] = []
    for row in payload:
        if not isinstance(row, dict):
            raise PublicBookError("trade_not_object")
        traded_ms = row.get("time")
        if isinstance(traded_ms, bool) or not isinstance(traded_ms, int):
            raise PublicBookError("bad_trade_time")
        buyer = row.get("isBuyerMaker")
        if not isinstance(buyer, bool):
            raise PublicBookError("bad_trade_flag")
        trades.append(
            PublicTrade(
                price=_decimal_string(row.get("price")),
                quantity=_decimal_string(row.get("qty")),
                traded_at=datetime.fromtimestamp(traded_ms / 1000, tz=timezone.utc),
                buyer_is_maker=buyer,
            )
        )
    return trades


def parse_klines(payload: object) -> list[Candle]:
    if not isinstance(payload, list):
        raise PublicBookError("klines_not_a_list")
    candles: list[Candle] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 6:
            raise PublicBookError("bad_kline")
        opened = row[0]
        if isinstance(opened, bool) or not isinstance(opened, int):
            raise PublicBookError("bad_kline_time")
        candles.append(
            Candle(
                open_time=datetime.fromtimestamp(opened / 1000, tz=timezone.utc),
                high=_decimal_string(row[2]),
                low=_decimal_string(row[3]),
                close=_decimal_string(row[4]),
                volume=_decimal_string(row[5]),
                open_price=_decimal_string(row[1]) if isinstance(row[1], str) else None,
            )
        )
    return candles


def parse_spot_filters(payload: object, base: str, quote: str) -> SpotFilters:
    _asset(base)
    _asset(quote)
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise PublicBookError("exchange_info_missing")
    wanted = f"{base.upper()}{quote.upper()}"
    match = None
    for row in payload["symbols"]:
        if isinstance(row, dict) and row.get("symbol") == wanted:
            match = row
            break
    if match is None:
        raise PublicBookError("symbol_not_listed")
    filters = match.get("filters")
    if not isinstance(filters, list):
        raise PublicBookError("filters_missing")
    tick = _filter_value(filters, "PRICE_FILTER", "tickSize")
    lot = _filter_value(filters, "LOT_SIZE", "stepSize")
    notional = _min_notional(filters)
    status = match.get("status")
    market_status = "trading" if status == "TRADING" else "halt"
    return SpotFilters(
        base=base.upper(),
        quote=quote.upper(),
        market_status=market_status,
        tick_size=tick,
        lot_size=lot,
        min_notional=notional,
    )


def _filter_value(filters: list[object], name: str, field: str) -> Decimal:
    for item in filters:
        if isinstance(item, dict) and item.get("filterType") == name:
            return _decimal_string(item.get(field))
    raise PublicBookError("filters_missing")


def _min_notional(filters: list[object]) -> Decimal:
    for name in ("NOTIONAL", "MIN_NOTIONAL"):
        for item in filters:
            if isinstance(item, dict) and item.get("filterType") == name:
                return _decimal_string(item.get("minNotional"))
    raise PublicBookError("filters_missing")


def _level(row: object) -> Level:
    if not isinstance(row, (list, tuple)) or len(row) < 2:
        raise PublicBookError("bad_level")
    return Level(_decimal_string(row[0]), _decimal_string(row[1]))


def _decimal_string(value: object) -> Decimal:
    if not isinstance(value, str):
        raise PublicBookError("level_not_decimal_string")
    try:
        return money(value)
    except MoneyError as exc:
        raise PublicBookError("level_not_decimal_string") from exc


def _require_uncrossed(book: OrderBook) -> None:
    try:
        book.spread_pct()
    except (ValueError, MoneyError) as exc:
        raise PublicBookError("crossed_or_invalid_book") from exc


def _asset(value: str) -> None:
    if not value.isascii() or not value.isalnum() or not 2 <= len(value) <= 10:
        raise PublicBookError("bad_asset")
