"""Deterministic replay with the production paper config: a 0.7% dip, a 1.2% recovery, one full
BUY -> SELL cycle with a partial fill and a restart in the middle. Grid parameters come from
configs/paper.binance_public.usdt.yaml unchanged."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.accounting.fees import exit_limit_price
from app.ai.analyst import MockAIAnalyst
from app.config import load_settings
from app.db.session import init_db, make_engine
from app.market.orderbook import Level, OrderBook
from app.news.events import EventStore
from app.news.ingest import NewsIngestor
from app.news.sources.mock import MockNewsSource
from app.paper.feed import ScriptedFeed
from app.paper.runtime import PaperRuntime
from app.schemas.orders import OrderState
from tests.conftest import NOW
from tests.test_stress import _identity

CONFIG = Path(__file__).resolve().parents[2] / "configs" / "paper.binance_public.usdt.yaml"
SYMBOL = "BTC/USDT"
STEP = timedelta(seconds=5)


def _settings():  # type: ignore[no-untyped-def]
    settings = load_settings(CONFIG)
    # Only the data source and the universe change; grid step, fees, slippage and limits are the file's.
    return settings.model_copy(update={"exchange": "simulated", "watchlist": ["BTC"], "max_active_pairs": 1})


def _engines() -> dict:
    engines = {name: make_engine("sqlite://") for name in ("shared", "baseline", "ai")}
    for engine in engines.values():
        init_db(engine)
    return engines


def _runtime(feed: ScriptedFeed, engines: dict) -> PaperRuntime:
    settings = _settings()
    news = NewsIngestor([MockNewsSource([])], EventStore(), MockAIAnalyst(), stale_after_sec=settings.news_stale_after_sec, watchlist=["BTC"])
    return PaperRuntime(settings, feed, news, engines=engines)


def _book(bid: str, ask: str, at: datetime, *, ask_qty: str = "10", bid_qty: str = "10") -> OrderBook:
    return OrderBook(symbol=SYMBOL, bids=[Level(Decimal(bid), Decimal(bid_qty))], asks=[Level(Decimal(ask), Decimal(ask_qty))], updated_at=at)


def _tick(runtime: PaperRuntime, feed: ScriptedFeed, at: datetime, bid: str, ask: str, **kw: str) -> None:
    feed.set_book(_book(bid, ask, at, **kw))
    runtime.tick(at)


def test_restart_keeps_the_grid_anchor_and_resting_prices() -> None:
    engines = _engines()
    feed = ScriptedFeed()
    first = _runtime(feed, engines)
    at = NOW
    _tick(first, feed, at, "99.98", "100.00")
    settings = first.settings
    expected_limit = Decimal("100.00") * (Decimal("1") - settings.grid_step_pct / Decimal("100"))
    for arm in first.arms.values():
        assert arm.strategy.anchors[SYMBOL] == Decimal("100.00")
        order = next(o for o in arm.adapter.orders.values() if o.intent.side == "buy")
        assert order.limit_price == expected_limit and order.state == OrderState.OPEN
    # The market moves up 1% while the process is down. Before the fix the grid re-anchored here.
    second = _runtime(feed, engines)
    for arm in second.arms.values():
        assert arm.strategy.anchors == {SYMBOL: Decimal("100.00")}
        assert set(arm.saved_orders) == {f"{arm.strategy.params.strategy_id}-BTC-buy-1"}
    at = at + STEP
    _tick(second, feed, at, "100.98", "101.00")
    assert second.errors == []
    assert second.restored_orders == 2
    for arm in second.arms.values():
        order = next(o for o in arm.adapter.orders.values() if o.intent.side == "buy")
        assert order.limit_price == expected_limit
        assert arm.ledger.reserved == settings.per_order_quote
    # Still the same after a second restart with no price change at all.
    third = _runtime(feed, engines)
    at = at + STEP
    _tick(third, feed, at, "100.98", "101.00")
    assert third.errors == []
    for arm in third.arms.values():
        assert next(o for o in arm.adapter.orders.values() if o.intent.side == "buy").limit_price == expected_limit
    snapshot = third.snapshot()
    assert snapshot["runtime"]["anchors"]["baseline"] == {SYMBOL: "100.00"}
    assert snapshot["runtime"]["restored_orders"] == 2


def test_anchor_follows_a_lower_fill_by_the_strategy_rule_not_by_restart() -> None:
    engines = _engines()
    feed = ScriptedFeed()
    runtime = _runtime(feed, engines)
    at = NOW
    _tick(runtime, feed, at, "99.98", "100.00")
    at = at + STEP
    _tick(runtime, feed, at, "99.20", "99.25")  # buy fills below the 99.50 limit
    for arm in runtime.arms.values():
        lot = next(iter(arm.ledger.lots.values()))
        assert arm.strategy.anchors[SYMBOL] == lot.entry_price  # strategy rule: anchor follows the fill
        assert lot.entry_price < Decimal("100.00")
    anchors = {name: dict(arm.strategy.anchors) for name, arm in runtime.arms.items()}
    again = _runtime(feed, engines)
    at = at + STEP
    _tick(again, feed, at, "99.60", "99.65")
    for name, arm in again.arms.items():
        assert arm.strategy.anchors == anchors[name]
    assert again.errors == []


def test_dip_then_recovery_closes_a_cycle_with_fees_partials_and_restart() -> None:
    engines = _engines()
    feed = ScriptedFeed()
    runtime = _runtime(feed, engines)
    settings = runtime.settings
    quote = settings.per_order_quote
    start_free = settings.starting_virtual_balance
    at = NOW

    # t0: anchor at 100.00, buy limit 99.50 (0.50% step from the config), 50 USDT reserved per arm.
    _tick(runtime, feed, at, "99.98", "100.00")
    for arm in runtime.arms.values():
        assert (arm.ledger.free, arm.ledger.reserved) == (start_free - quote, quote)

    # t1: -0.7% but only 0.2 BTC on the ask: a partial fill (~19.9 USDT of the 50 budget).
    at = at + STEP
    _tick(runtime, feed, at, "99.28", "99.30", ask_qty="0.2")
    for arm in runtime.arms.values():
        order = next(o for o in arm.adapter.orders.values() if o.intent.side == "buy")
        assert order.state == OrderState.PARTIAL
        assert order.filled_base == Decimal("0.2")
        assert 0 < order.remaining_quote < quote
        fill = arm.ledger.fills[f"{order.client_order_id}-fill-1"]
        assert fill.side == "buy" and fill.quantity == Decimal("0.2")
        assert fill.price == Decimal("99.30") * (Decimal("1") + settings.assumed_slippage_pct / Decimal("100"))
        assert fill.fee_amount == fill.price * fill.quantity * settings.assumed_taker_fee_pct / Decimal("100")
        assert fill.quote_amount == fill.price * fill.quantity + fill.fee_amount
        assert arm.ledger.free == start_free - quote
        assert arm.ledger.reserved == quote - fill.quote_amount
        # The exit for the partial lot is armed immediately.
        assert any(o.intent.side == "sell" and o.state == OrderState.OPEN for o in arm.adapter.orders.values())
    _identity(runtime)

    # Same book again: idempotent, nothing doubles.
    before = {name: (arm.ledger.free, arm.ledger.reserved, len(arm.ledger.fills), len(arm.ledger.lots)) for name, arm in runtime.arms.items()}
    at = at + STEP
    _tick(runtime, feed, at, "99.28", "99.30", ask_qty="0")
    for name, arm in runtime.arms.items():
        assert (arm.ledger.free, arm.ledger.reserved, len(arm.ledger.fills), len(arm.ledger.lots)) == before[name]

    # Restart in the middle of the partial fill.
    restarted = _runtime(feed, engines)
    at = at + STEP
    _tick(restarted, feed, at, "99.28", "99.30", ask_qty="0")
    assert restarted.errors == []
    for name, arm in restarted.arms.items():
        assert (arm.ledger.free, arm.ledger.reserved, len(arm.ledger.fills), len(arm.ledger.lots)) == before[name]
        order = next(o for o in arm.adapter.orders.values() if o.intent.side == "buy")
        assert order.state == OrderState.PARTIAL and order.fill_ids == [f"{order.client_order_id}-fill-1"]
        assert order.filled_quote == arm.ledger.fills[order.fill_ids[0]].quote_amount
    runtime = restarted

    # t2: liquidity returns at the dip; the rest of the budget fills as fill-2 / lot-2.
    at = at + STEP
    _tick(runtime, feed, at, "99.28", "99.30")
    for arm in runtime.arms.values():
        order = next(o for o in arm.adapter.orders.values() if o.intent.side == "buy")
        assert order.state == OrderState.FILLED
        assert order.fill_ids == [f"{order.client_order_id}-fill-1", f"{order.client_order_id}-fill-2"]
        assert len(arm.ledger.lots) == 2
        spent = sum((f.quote_amount for f in arm.ledger.fills.values() if f.side == "buy"), Decimal("0"))
        assert spent <= quote
        # The first entry is fully consumed (dust under 1e-6 returned); the grid immediately
        # reserves the next level one step under the new, lower anchor.
        assert arm.ledger.reservations[f"{arm.strategy.params.strategy_id}-BTC-buy-1"].status in {"consumed", "released"}
        next_buy = next(o for o in arm.adapter.orders.values() if o.intent.side == "buy" and o.state == OrderState.OPEN)
        assert next_buy.client_order_id.endswith("-BTC-buy-3")  # two lots exist -> level 3
        assert next_buy.limit_price < Decimal("99.30")
        assert arm.ledger.reserved == quote
        assert arm.ledger.free == start_free - spent - quote
        exits = [o for o in arm.adapter.orders.values() if o.intent.side == "sell" and o.state == OrderState.OPEN]
        assert len(exits) == 2
        for lot in arm.ledger.lots.values():
            expected_exit = exit_limit_price(
                entry_price=lot.entry_price,
                entry_fee_pct=lot.entry_fee_amount / (lot.entry_price * lot.gross_base_qty) * Decimal("100"),
                exit_fee_pct=settings.assumed_maker_fee_pct,
                slippage_pct=settings.assumed_slippage_pct,
                net_target_pct=settings.net_target_pct,
            )
            exit_order = next(o for o in exits if o.intent.lot_id == lot.lot_id)
            assert exit_order.limit_price == expected_exit
            assert expected_exit > lot.entry_price
    _identity(runtime)

    # t3: +0.6% from the dip is not enough for the exit limit (~+0.35% above entry plus slippage).
    at = at + STEP
    _tick(runtime, feed, at, "99.60", "99.62")
    exits_limit = min(o.limit_price for o in runtime.arms["baseline"].adapter.orders.values() if o.intent.side == "sell")
    assert Decimal("99.60") * (Decimal("1") - settings.assumed_slippage_pct / Decimal("100")) < exits_limit
    for arm in runtime.arms.values():
        assert all(lot.status != "closed" for lot in arm.ledger.lots.values())

    # t4: +1.2% from the dip: both lots sell, cycle closed.
    at = at + STEP
    recovery = (Decimal("99.30") * Decimal("1.012")).quantize(Decimal("0.01"))
    _tick(runtime, feed, at, str(recovery), str(recovery + Decimal("0.02")))
    for arm in runtime.arms.values():
        lots = list(arm.ledger.lots.values())
        assert all(lot.status == "closed" for lot in lots) and len(lots) == 2
        buys = [f for f in arm.ledger.fills.values() if f.side == "buy"]
        sells = [f for f in arm.ledger.fills.values() if f.side == "sell"]
        assert len(buys) == 2 and len(sells) == 2
        spent = sum((f.quote_amount for f in buys), Decimal("0"))  # buy fill: notional + fee
        received = sum((f.price * f.quantity - f.fee_amount for f in sells), Decimal("0"))  # net proceeds
        fees = sum((f.fee_amount for f in buys + sells), Decimal("0"))
        assert arm.ledger.realized_pnl == received - spent
        # A sell FillRecord stores the realized result of that fill in quote_amount (ledger convention).
        assert sum((f.quote_amount for f in sells), Decimal("0")) == arm.ledger.realized_pnl
        assert arm.ledger.realized_pnl > 0
        assert arm.ledger.fees_by_currency == {"USDT": fees}
        assert fees > 0
        # All sells happened at the bid less slippage, above every exit limit.
        for fill in sells:
            assert fill.price == recovery * (Decimal("1") - settings.assumed_slippage_pct / Decimal("100"))
        # The next grid level is still resting below the market; everything else is free cash.
        resting = [o for o in arm.adapter.orders.values() if o.intent.side == "buy" and o.state == OrderState.OPEN]
        assert len(resting) == 1 and arm.ledger.reserved == quote
        assert arm.ledger.free == start_free + arm.ledger.realized_pnl - quote
        marked = arm.ledger.equity({SYMBOL: recovery}, settings.assumed_taker_fee_pct)
        assert marked.equity == start_free + arm.ledger.realized_pnl
        assert marked.unrealized_pnl == Decimal("0")
        # Net result is below the gross price move: fees and slippage are paid.
        gross_move = spent * (recovery / Decimal("99.30") - 1)
        assert Decimal("0") < arm.ledger.realized_pnl < gross_move
    _identity(runtime)
    assert runtime.arms["baseline"].ledger.realized_pnl == runtime.arms["ai"].ledger.realized_pnl
    assert Decimal(runtime.snapshot()["ab"]["equity_delta"]) == 0

    # The closed cycle survives a final restart intact.
    final = _runtime(feed, engines)
    for name, arm in final.arms.items():
        assert arm.ledger.realized_pnl == runtime.arms[name].ledger.realized_pnl
        assert arm.ledger.free == runtime.arms[name].ledger.free
        assert all(lot.status == "closed" for lot in arm.ledger.lots.values())
