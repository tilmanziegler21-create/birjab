"""Offline candle replay through the same Ledger, RiskManager and GridStrategy.

Conservative intra-bar model: on each candle we first try exits against the high, then
entries against the low. A lot opened on this bar cannot sell on the same bar.
A candle print is not a guarantee that both sides of the grid filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.accounting.pnl import MissingMark, drawdown_pct
from app.config import Settings
from app.db.session import init_db, make_engine
from app.market.orderbook import Level, OrderBook
from app.market.public import Candle
from app.paper.feed import ScriptedFeed
from app.paper.runtime import PaperRuntime
DEPTH = Decimal("100")


def memory_engines() -> dict:
    engines = {name: make_engine("sqlite://") for name in ("shared", "baseline", "ai")}
    for engine in engines.values():
        init_db(engine)
    return engines


def candle_book(symbol: str, candle: Candle, *, phase: str, spread_pct: Decimal) -> OrderBook:
    half = spread_pct / Decimal("200")
    if phase == "sell":
        bid = candle.high
        ask = max(candle.high * (Decimal("1") + half), candle.high + Decimal("0.00000001"))
    else:
        ask = candle.low
        bid = min(candle.low * (Decimal("1") - half), candle.low)
        if bid <= 0:
            bid = candle.low
    return OrderBook(
        symbol=symbol,
        bids=[Level(bid, DEPTH)],
        asks=[Level(ask, DEPTH)],
        updated_at=candle.open_time,
    )


@dataclass
class ReplayResult:
    step_pct: Decimal
    ticks: int
    candles: int
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    free: Decimal
    reserved: Decimal
    profit_reserve: Decimal
    open_lots: int
    completed_cycles: int
    buys: int
    sells: int
    fees: dict[str, Decimal]
    drawdown_pct: Decimal
    adaptive: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_pct": str(self.step_pct),
            "adaptive": self.adaptive,
            "ticks": self.ticks,
            "candles": self.candles,
            "equity": str(self.equity),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "free": str(self.free),
            "reserved": str(self.reserved),
            "profit_reserve": str(self.profit_reserve),
            "open_lots": self.open_lots,
            "completed_cycles": self.completed_cycles,
            "buys": self.buys,
            "sells": self.sells,
            "fees": {k: str(v) for k, v in self.fees.items()},
            "drawdown_pct": str(self.drawdown_pct),
            "note": self.note,
        }


def replay_candles(
    settings: Settings,
    candles: list[Candle],
    symbol: str,
    *,
    step_pct: Decimal | None = None,
    adaptive_step: Decimal | None = None,
    engines: dict | None = None,
) -> ReplayResult:
    """Run an isolated PaperRuntime. Callers must pass memory engines, never the live DB."""

    if engines is None:
        engines = memory_engines()
    feed = ScriptedFeed()
    from app.ai.analyst import MockAIAnalyst
    from app.news.events import EventStore
    from app.news.ingest import NewsIngestor
    from app.news.sources.mock import MockNewsSource

    news = NewsIngestor(
        [MockNewsSource([])],
        EventStore(),
        MockAIAnalyst(),
        stale_after_sec=settings.news_stale_after_sec,
        watchlist=[symbol.split("/")[0]],
    )
    updates: dict[str, object] = {
        "exchange": "simulated",
        "watchlist": [symbol.split("/")[0]],
        "max_active_pairs": 1,
        "run_loop": False,
    }
    used_step = settings.grid_step_pct
    adaptive = False
    if adaptive_step is not None:
        used_step = adaptive_step
        adaptive = True
        updates["grid_step_pct"] = adaptive_step
    elif step_pct is not None:
        used_step = step_pct
        updates["grid_step_pct"] = step_pct
    runtime = PaperRuntime(settings.model_copy(update=updates), feed, news, engines=engines)
    spread = settings.max_spread_pct if settings.max_spread_pct > 0 else Decimal("0.05")
    last_bid = None
    for candle in candles:
        sell_book = candle_book(symbol, candle, phase="sell", spread_pct=spread)
        feed.set_book(sell_book)
        runtime.tick(candle.open_time)
        buy_book = candle_book(symbol, candle, phase="buy", spread_pct=spread)
        feed.set_book(buy_book)
        runtime.tick(candle.open_time)
        last_bid = candle.close
    arm = runtime.arms["baseline"]
    ledger = arm.ledger
    try:
        marked = ledger.equity({symbol: last_bid} if last_bid is not None else {}, settings.assumed_taker_fee_pct)
        equity = marked.equity
        unreal = marked.unrealized_pnl
    except MissingMark:
        equity = ledger.free + ledger.reserved + ledger.profit_reserve
        unreal = Decimal("0")
    fills = list(ledger.fills.values())
    try:
        dd = drawdown_pct(peak=ledger.peak_equity, current=equity) if ledger.peak_equity > 0 else Decimal("0")
    except ValueError:
        dd = Decimal("0")
    return ReplayResult(
        step_pct=used_step,
        ticks=runtime.tick_count,
        candles=len(candles),
        equity=equity,
        realized_pnl=ledger.realized_pnl,
        unrealized_pnl=unreal,
        free=ledger.free,
        reserved=ledger.reserved,
        profit_reserve=ledger.profit_reserve,
        open_lots=sum(1 for lot in ledger.lots.values() if lot.remaining_base_qty > 0),
        completed_cycles=sum(1 for lot in ledger.lots.values() if lot.status == "closed"),
        buys=sum(1 for f in fills if f.side == "buy"),
        sells=sum(1 for f in fills if f.side == "sell"),
        fees=dict(ledger.fees_by_currency),
        drawdown_pct=dd,
        adaptive=adaptive,
        note="conservative: sell-phase then buy-phase; no same-bar round trip; not a live result",
    )
