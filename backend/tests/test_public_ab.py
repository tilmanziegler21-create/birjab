from __future__ import annotations

import ast
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.exchange import LiveAdapter, LiveTradingDisabledError
from app.market.features import atr_pct
from app.market.http_get import read_only_get
from app.market.orderbook import Level, OrderBook
from app.market.public import (
    PublicBookClient,
    PublicBookError,
    parse_book_frame,
    parse_depth,
    parse_klines,
    parse_spot_filters,
    parse_trades,
)
from app.market.universe import MarketFilterInput, UniversePolicy, hard_filter
from app.money import QuoteCurrency
from app.research.compare_ab import Tick, run_ab
from app.schemas.risk import RiskRegime
from app.strategy.grid import GridParams
from tests.conftest import NOW, policy

ROOT = Path(__file__).resolve().parents[2]
MARKET = Path(__file__).resolve().parents[1] / "app" / "market"
RESEARCH = Path(__file__).resolve().parents[1] / "app" / "research"


def _book(bid: str, ask: str, now: object = None) -> OrderBook:
    return OrderBook(
        symbol="BTC/USDC",
        bids=[Level(Decimal(bid), Decimal("10"))],
        asks=[Level(Decimal(ask), Decimal("10"))],
        updated_at=now if now is not None else NOW,  # type: ignore[arg-type]
    )


def _params(**overrides: object) -> GridParams:
    values: dict[str, object] = {
        "strategy_id": "baseline_grid",
        "per_order_quote": Decimal("50"),
        "grid_step_pct": Decimal("1.00"),
        "net_target_pct": Decimal("0.10"),
        "maker_fee_pct": Decimal("0.10"),
        "taker_fee_pct": Decimal("0.10"),
        "slippage_pct": Decimal("0"),
        "min_depth_multiplier": Decimal("10"),
        "quote_currency": "USDC",
    }
    values.update(overrides)
    return GridParams(**values)  # type: ignore[arg-type]


def _depth(bid: str = "99.98", ask: str = "100.00") -> bytes:
    return json.dumps({"bids": [[bid, "10"]], "asks": [[ask, "10"]]}).encode()


def test_public_depth_is_a_get_without_a_key_and_eur_is_not_rewritten() -> None:
    seen: list[str] = []

    def transport(url: str) -> bytes:
        seen.append(url)
        return _depth()

    client = PublicBookClient(transport)
    book = client.fetch_depth("BTC", "USDC", NOW)
    assert seen == ["https://api.binance.com/api/v3/depth?symbol=BTCUSDC&limit=20"]
    assert "apiKey" not in seen[0]
    assert book.best_bid() == Decimal("99.98")
    assert book.best_ask() == Decimal("100.00")
    assert book.symbol == "BTC/USDC"

    def eur(url: str) -> bytes:
        assert "BTCEUR" in url
        assert "USDT" not in url
        return _depth("99.90", "100.00")

    eur_book = PublicBookClient(eur).fetch_depth("btc", "eur", NOW)
    assert eur_book.symbol == "BTC/EUR"


def test_order_url_and_float_levels_are_refused() -> None:
    def transport(url: str) -> bytes:
        raise AssertionError(url)

    client = PublicBookClient(transport)
    with pytest.raises(PublicBookError):
        client.fetch_depth("BTC", "USDC", NOW, limit=7)
    assert client.requests == []

    with pytest.raises(PublicBookError):
        read_only_get("https://api.binance.com/api/v3/order", opener=transport)
    with pytest.raises(PublicBookError):
        read_only_get(
            "https://api.binance.com/api/v3/depth?symbol=BTCUSDC&signature=abc",
            opener=transport,
        )
    with pytest.raises(PublicBookError):
        parse_depth({"bids": [[100.0, "1"]], "asks": [["101", "1"]]}, "BTC/USDC", NOW)
    with pytest.raises(PublicBookError):
        parse_depth({"bids": [["101", "1"]], "asks": [["100", "1"]]}, "BTC/USDC", NOW)
    with pytest.raises(PublicBookError):
        parse_depth([[0, "100", "101", "99", "100", "1"]], "BTC/USDC", NOW)


def test_read_only_get_sends_get_and_no_key_header() -> None:
    captured: list[object] = []

    def opener(request: object, timeout: float = 0) -> object:
        del timeout
        captured.append(request)

        class Response:
            def read(self) -> bytes:
                return _depth()

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return Response()

    body = read_only_get("https://api.binance.com/api/v3/depth?symbol=BTCUSDC&limit=5", opener=opener)
    assert body == _depth()
    request = captured[0]
    assert request.get_method() == "GET"  # type: ignore[attr-defined]
    headers = {key.lower(): value for key, value in request.header_items()}  # type: ignore[attr-defined]
    assert "x-mbx-apikey" not in headers
    assert "apiKey" not in request.full_url  # type: ignore[attr-defined]


def test_klines_feed_atr_and_are_not_a_book() -> None:
    rows = []
    for index in range(15):
        rows.append([1_700_000_000_000 + index, "100", "101", "99", "100", "1"])
    candles = parse_klines(rows)
    assert atr_pct(candles) == Decimal("2")
    with pytest.raises(ValueError, match="not_enough_candles"):
        atr_pct(candles[:3])
    with pytest.raises(PublicBookError):
        parse_depth(rows, "BTC/USDC", NOW)


def test_partial_book_frame_keeps_latency_and_a_delta_is_not_a_book() -> None:
    event_ms = int(NOW.timestamp()) * 1000 - 1000
    frame = json.dumps(
        {
            "stream": "btcusdc@depth10@100ms",
            "data": {"E": event_ms, "bids": [["99.98", "10"]], "asks": [["100.00", "10"]]},
        }
    )
    timed = parse_book_frame(frame, symbol="BTC/USDC", received_at=NOW, stale_after_sec=15)
    assert timed.book.symbol == "BTC/USDC"
    assert timed.latency_ms == 1000
    assert timed.stale is False
    old_ms = int(NOW.timestamp()) * 1000 - 20_000
    stale = parse_book_frame(
        json.dumps({"E": old_ms, "bids": [["99.98", "10"]], "asks": [["100.00", "10"]]}),
        symbol="BTC/USDC",
        received_at=NOW,
        stale_after_sec=15,
    )
    assert stale.stale is True
    with pytest.raises(PublicBookError, match="partial_delta_not_a_book"):
        parse_book_frame(
            json.dumps({"e": "depthUpdate", "b": [["99", "1"]], "a": [["100", "1"]]}),
            symbol="BTC/USDC",
            received_at=NOW,
            stale_after_sec=15,
        )


def test_trades_and_filters_do_not_invent_a_fillable_book() -> None:
    trades = parse_trades(
        [{"price": "100.00", "qty": "0.01", "time": 1_700_000_000_000, "isBuyerMaker": False}]
    )
    assert trades[0].price == Decimal("100.00")
    assert not hasattr(trades[0], "bids")
    filters = parse_spot_filters(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDC",
                    "status": "BREAK",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
                        {"filterType": "NOTIONAL", "minNotional": "5"},
                    ],
                }
            ]
        },
        "BTC",
        "USDC",
    )
    decision = hard_filter(
        MarketFilterInput(
            base_asset="BTC",
            quote_currency=QuoteCurrency.USDC,
            market_status=filters.market_status,  # type: ignore[arg-type]
            bid=Decimal("99.98"),
            ask=Decimal("100.00"),
            order_quote=Decimal("50"),
            order_qty=Decimal("0.00050"),
            order_price=Decimal("100.00"),
            ask_depth_quote=Decimal("1000"),
            bid_depth_quote=Decimal("1000"),
            min_notional=filters.min_notional,
            tick_size=filters.tick_size,
            lot_size=filters.lot_size,
        ),
        UniversePolicy(
            quote_currency=QuoteCurrency.USDC,
            watchlist=["BTC"],
            min_depth_multiplier=Decimal("10"),
            max_spread_pct=Decimal("0.50"),
        ),
    )
    assert "market_not_trading" in decision.reasons
    with pytest.raises(PublicBookError):
        parse_spot_filters({"symbols": []}, "SOL", "EUR")


def test_same_tape_same_equity_and_the_example_step_enters_neither_arm() -> None:
    book = _book("99.98", "100")
    same = run_ab([Tick(book, NOW)], _params(), policy())
    assert same.baseline.equity == same.ai.equity == Decimal("5000")
    assert same.equity_delta == 0
    assert same.saved == ()
    assert same.missed == ()
    assert book.asks[0].base_qty == Decimal("10")

    tight = _params(
        grid_step_pct=Decimal("0.30"),
        maker_fee_pct=Decimal("0.10"),
        taker_fee_pct=Decimal("0.10"),
        slippage_pct=Decimal("0.05"),
        net_target_pct=Decimal("0.10"),
    )
    skipped = run_ab([Tick(_book("99.98", "100"), NOW)], tight, policy())
    assert skipped.baseline.entries_allowed == 0
    assert skipped.ai.entries_allowed == 0
    assert skipped.baseline.equity == skipped.ai.equity == Decimal("5000")


def test_ai_pause_saves_the_crash_and_misses_the_rebound() -> None:
    pause = dict(
        regime=RiskRegime.PAUSE_BUYS,
        event_id="example-incident-001",
        source_urls=("https://example.org/illustrative-source",),
        published_at=NOW - timedelta(seconds=2),
    )
    crash = run_ab(
        [
            Tick(_book("99.98", "100", NOW), NOW, **pause),
            Tick(_book("98.98", "99", NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1), **pause),
            Tick(_book("90", "90.05", NOW + timedelta(seconds=2)), NOW + timedelta(seconds=2), **pause),
        ],
        _params(),
        policy(),
    )
    assert crash.ai.equity == Decimal("5000")
    assert crash.baseline.equity < Decimal("5000")
    assert crash.equity_delta > 0
    assert crash.baseline.buy_fills >= 1
    assert crash.ai.buy_fills == 0
    assert crash.ai.paused_entry_pct == Decimal("100")
    assert "ai_filtered_grid-BTC-buy-1" in crash.saved
    assert crash.missed == ()
    assert crash.independent_events == 1
    assert crash.news_latency_ms == (2000, 3000, 4000)
    assert crash.baseline.max_drawdown_pct > 0
    assert crash.ai.max_drawdown_pct == 0
    rejected = [item for item in crash.decisions if item.arm == "ai" and not item.allowed]
    assert rejected[0].reason == "news_regime_pause_buys"
    assert rejected[0].source_urls == ("https://example.org/illustrative-source",)

    rebound = run_ab(
        [
            Tick(_book("99.98", "100", NOW), NOW, **pause),
            Tick(_book("98.98", "99", NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1), **pause),
            Tick(_book("102", "102.05", NOW + timedelta(seconds=2)), NOW + timedelta(seconds=2), **pause),
        ],
        _params(),
        policy(),
    )
    assert rebound.baseline.equity > Decimal("5000")
    assert rebound.ai.equity == Decimal("5000")
    assert rebound.equity_delta < 0
    assert rebound.baseline.completed_cycles >= 1
    assert rebound.ai.completed_cycles == 0
    assert "ai_filtered_grid-BTC-buy-1" in rebound.missed
    assert rebound.saved == ()
    assert rebound.baseline.realized_pnl > 0
    assert rebound.ai.realized_pnl == 0


def test_stale_public_book_blocks_both_arms_and_live_still_cannot_send() -> None:
    stale_book = _book("99.98", "100", NOW - timedelta(seconds=16))
    report = run_ab([Tick(stale_book, NOW, regime=RiskRegime.PAUSE_BUYS)], _params(), policy())
    assert report.baseline.entries_allowed == 0
    assert report.ai.entries_allowed == 0
    assert report.baseline.equity == report.ai.equity == Decimal("5000")
    assert {item.reason for item in report.decisions} == {"stale_market_feed"}
    with pytest.raises(LiveTradingDisabledError):
        LiveAdapter().place_order()


def test_market_modules_do_not_grow_an_order_client() -> None:
    banned = {"socket", "httpx", "requests", "ccxt", "websockets", "aiohttp", "openai"}
    for path in list(MARKET.rglob("*.py")) + list(RESEARCH.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec"}
        if path.name != "http_get.py":
            assert "urllib" not in imported
        assert imported.isdisjoint(banned)
    assert "api.binance.com" in (MARKET / "public.py").read_text(encoding="utf-8")
    assert "place_order" not in (MARKET / "public.py").read_text(encoding="utf-8")
    assert ROOT.joinpath("configs", "paper.example.yaml").is_file()
