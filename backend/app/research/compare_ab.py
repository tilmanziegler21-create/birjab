"""Paper A/B on one tape. The score is equity after costs, not closed cycles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.accounting.ledger import Ledger
from app.accounting.pnl import drawdown_pct
from app.adapters.paper import PaperAdapter
from app.market.orderbook import OrderBook
from app.money import money, percent_of
from app.risk.manager import RiskManager, RiskPolicy
from app.schemas.risk import RiskRegime
from app.strategy.grid import AIFilteredGridStrategy, BaseGridStrategy, GridParams, context_from_book

_NEWS_PREFIX = "news_"


@dataclass(frozen=True)
class Tick:
    book: OrderBook
    now: datetime
    regime: RiskRegime = RiskRegime.NORMAL
    news_last_ok_at: datetime | None = None
    event_id: str | None = None
    source_urls: tuple[str, ...] = ()
    published_at: datetime | None = None


@dataclass(frozen=True)
class DecisionLog:
    arm: str
    at: datetime
    client_order_id: str
    side: str
    allowed: bool
    reason: str
    symbol: str
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class ArmReport:
    name: str
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_quote: Decimal
    max_drawdown_pct: Decimal
    buy_fills: int
    entries_allowed: int
    entries_rejected: int
    paused_entry_pct: Decimal
    completed_cycles: int
    free_quote: Decimal
    reserved_quote: Decimal


@dataclass(frozen=True)
class Comparison:
    baseline: ArmReport
    ai: ArmReport
    equity_delta: Decimal
    saved: tuple[str, ...]
    missed: tuple[str, ...]
    independent_events: int
    news_latency_ms: tuple[int, ...]
    decisions: tuple[DecisionLog, ...]


def run_ab(
    ticks: list[Tick],
    params: GridParams,
    policy: RiskPolicy,
    *,
    starting_balance: str = "5000",
) -> Comparison:
    if not ticks:
        raise ValueError("empty_tape")
    baseline = _arm("baseline", BaseGridStrategy, params, policy, starting_balance)
    ai = _arm("ai", AIFilteredGridStrategy, params, policy, starting_balance)
    decisions: list[DecisionLog] = []
    latencies: list[int] = []
    events: set[str] = set()
    for tick in ticks:
        if tick.event_id:
            events.add(tick.event_id)
        if tick.published_at is not None:
            if tick.published_at.tzinfo is None or tick.now.tzinfo is None:
                raise ValueError("naive_timestamp")
            latencies.append(int((tick.now - tick.published_at).total_seconds() * 1000))
        decisions.extend(_step(baseline, tick, params))
        decisions.extend(_step(ai, tick, params))
    last = ticks[-1].book
    base_report = _report(baseline, last, params)
    ai_report = _report(ai, last, params)
    saved, missed = _counterfactual(baseline, ai, last)
    return Comparison(
        baseline=base_report,
        ai=ai_report,
        equity_delta=ai_report.equity - base_report.equity,
        saved=saved,
        missed=missed,
        independent_events=len(events),
        news_latency_ms=tuple(latencies),
        decisions=tuple(decisions),
    )


@dataclass
class _Arm:
    name: str
    ledger: Ledger
    adapter: PaperAdapter
    strategy: BaseGridStrategy


def _arm(
    name: str,
    kind: type[BaseGridStrategy],
    params: GridParams,
    policy: RiskPolicy,
    starting_balance: str,
) -> _Arm:
    ledger = Ledger()
    ledger.open_account(params.quote_currency, Decimal(starting_balance))
    manager = RiskManager(ledger, policy)
    adapter = PaperAdapter(
        ledger,
        manager,
        maker_fee_pct=params.maker_fee_pct,
        taker_fee_pct=params.taker_fee_pct,
        slippage_pct=params.slippage_pct,
    )
    return _Arm(name=name, ledger=ledger, adapter=adapter, strategy=kind(params))


def _step(arm: _Arm, tick: Tick, params: GridParams) -> list[DecisionLog]:
    before = len(arm.adapter.decisions)
    fresh = tick.now if tick.news_last_ok_at is None else tick.news_last_ok_at
    ctx = context_from_book(
        tick.book,
        now=tick.now,
        regime=tick.regime,
        news_last_ok_at=fresh,
    )
    arm.strategy.sync(arm.adapter, tick.book, ctx)
    arm.ledger.mark_equity({tick.book.symbol: tick.book.best_bid()}, params.taker_fee_pct)
    logs: list[DecisionLog] = []
    for submission in arm.adapter.decisions[before:]:
        logs.append(
            DecisionLog(
                arm=arm.name,
                at=tick.now,
                client_order_id=submission.decision.client_order_id or "",
                side=submission.order.intent.side if submission.order is not None else _side(submission),
                allowed=submission.decision.allowed,
                reason=submission.decision.reason,
                symbol=tick.book.symbol,
                source_urls=tick.source_urls,
            )
        )
    return logs


def _side(submission: object) -> str:
    decision = getattr(submission, "decision", None)
    client_order_id = getattr(decision, "client_order_id", "") or ""
    if "-sell" in client_order_id:
        return "sell"
    return "buy"


def _report(arm: _Arm, book: OrderBook, params: GridParams) -> ArmReport:
    marked = arm.ledger.mark_equity({book.symbol: book.best_bid()}, params.taker_fee_pct)
    buys = _first_buys(arm)
    allowed = [item for item in buys if item.allowed]
    rejected = [item for item in buys if not item.allowed]
    paused = [item for item in rejected if item.reason.startswith(_NEWS_PREFIX)]
    attempts = len(allowed) + len(rejected)
    buy_fills = sum(1 for order in arm.adapter.orders.values() if order.intent.side == "buy" and order.fill_ids)
    cycles = sum(1 for lot in arm.ledger.lots.values() if lot.status == "closed")
    quote = params.quote_currency
    return ArmReport(
        name=arm.name,
        equity=marked.equity,
        realized_pnl=marked.realized_pnl,
        unrealized_pnl=marked.unrealized_pnl,
        fees_quote=marked.fees_by_currency.get(quote, Decimal("0")),
        max_drawdown_pct=drawdown_pct(peak=arm.ledger.peak_equity, current=marked.equity),
        buy_fills=buy_fills,
        entries_allowed=len(allowed),
        entries_rejected=len(rejected),
        paused_entry_pct=(Decimal(len(paused)) / Decimal(attempts) * Decimal("100")) if attempts else Decimal("0"),
        completed_cycles=cycles,
        free_quote=marked.free_quote,
        reserved_quote=marked.reserved_quote,
    )


def _first_buys(arm: _Arm) -> list[DecisionLog]:
    found: dict[str, DecisionLog] = {}
    for submission in arm.adapter.decisions:
        if submission.decision.reason == "idempotent":
            continue
        client_order_id = submission.decision.client_order_id or ""
        side = submission.order.intent.side if submission.order is not None else "buy"
        if side != "buy" and "-sell" in client_order_id:
            continue
        if submission.order is not None and submission.order.intent.side != "buy":
            continue
        if client_order_id in found:
            continue
        found[client_order_id] = DecisionLog(
            arm=arm.name,
            at=datetime.min,
            client_order_id=client_order_id,
            side="buy",
            allowed=submission.decision.allowed,
            reason=submission.decision.reason,
            symbol="",
            source_urls=(),
        )
    return list(found.values())


def _counterfactual(
    baseline: _Arm, ai: _Arm, book: OrderBook
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    saved: list[str] = []
    missed: list[str] = []
    seen: set[str] = set()
    for submission in ai.adapter.decisions:
        if submission.decision.allowed or not (submission.decision.reason or "").startswith(_NEWS_PREFIX):
            continue
        client_order_id = submission.decision.client_order_id or ""
        if not client_order_id or client_order_id in seen:
            continue
        if "-sell" in client_order_id:
            continue
        seen.add(client_order_id)
        lot_id = client_order_id.replace("ai_filtered_grid", "baseline_grid", 1) + "-lot-1"
        lot = baseline.ledger.lots.get(lot_id)
        if lot is None:
            continue
        net = _lot_net(baseline, lot_id, book)
        if net < 0:
            saved.append(client_order_id)
        elif net > 0:
            missed.append(client_order_id)
    return tuple(saved), tuple(missed)


def _lot_net(arm: _Arm, lot_id: str, book: OrderBook) -> Decimal:
    lot = arm.ledger.lots[lot_id]
    if lot.status == "closed":
        return money(lot.realized_pnl)
    if lot.symbol != book.symbol:
        raise ValueError("missing_mark")
    gross = book.best_bid() * lot.remaining_base_qty
    fee = percent_of(gross, arm.adapter.taker_fee_pct)
    return (gross - fee) - lot.quote_cost_remaining
