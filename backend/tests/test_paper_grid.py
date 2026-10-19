from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.accounting.fees import exit_limit_price
from app.adapters.exchange import LiveAdapter, LiveTradingDisabledError
from app.adapters.paper import PaperAdapter
from app.db.repository import load, save
from app.db.session import init_db, make_engine, make_session_factory
from app.execution.orders import OrderState
from app.market.orderbook import Level, OrderBook, match_buy
from app.risk.manager import RiskManager
from app.schemas.orders import OrderIntent
from app.schemas.risk import RiskRegime
from app.strategy.grid import (
    AIFilteredGridStrategy,
    BaseGridStrategy,
    GridParams,
    context_from_book,
)
from tests.conftest import open_ledger, policy

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _book(bid: str, ask: str, *, bid_qty: str = "10", ask_qty: str = "10") -> OrderBook:
    return OrderBook(
        symbol="BTC/USDC",
        bids=[Level(Decimal(bid), Decimal(bid_qty))],
        asks=[Level(Decimal(ask), Decimal(ask_qty))],
        updated_at=NOW,
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


def _adapter(ledger: object | None = None, **fees: Decimal) -> tuple[object, PaperAdapter]:
    account = ledger or open_ledger()
    manager = RiskManager(account, policy(per_order_quote=Decimal("50")))  # type: ignore[arg-type]
    adapter = PaperAdapter(account, manager, **fees)  # type: ignore[arg-type]
    return account, adapter


def _buy(limit: str = "100", quote: str = "50", client_order_id: str = "buy-1") -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        symbol="BTC/USDC",
        side="buy",
        purpose="entry",
        quote_value=Decimal(quote),
        limit_price=Decimal(limit),
        reason="paper buy",
        strategy_id="baseline_grid",
    )


def test_match_does_not_fill_through_the_limit_or_past_depth() -> None:
    asks = [Level(Decimal("100"), Decimal("0.2"))]
    blocked = match_buy(
        asks,
        limit_price=Decimal("100"),
        quote_budget=Decimal("50"),
        fee_pct=Decimal("0"),
        slippage_pct=Decimal("1"),
        fee_currency="USDC",
    )
    assert blocked == []
    assert asks[0].base_qty == Decimal("0.2")
    filled = match_buy(
        [Level(Decimal("100"), Decimal("0.2"))],
        limit_price=Decimal("100"),
        quote_budget=Decimal("50"),
        fee_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
        fee_currency="USDC",
    )
    assert len(filled) == 1
    assert filled[0].quantity == Decimal("0.2")
    assert filled[0].quote_amount == Decimal("20")


def test_partial_fill_then_rest_of_budget_and_duplicate_order() -> None:
    ledger, adapter = _adapter(maker_fee_pct=Decimal("0"), taker_fee_pct=Decimal("0"))
    adapter.update_book(_book("99.98", "100", ask_qty="0.2"))
    ctx = context_from_book(_book("99.98", "100", ask_qty="0.2"), now=NOW)
    first = adapter.place_order(_buy(), ctx)
    assert first.order is not None
    assert first.order.state == OrderState.PARTIAL
    assert len(ledger.lots) == 1
    assert ledger.reserved == Decimal("30")
    again = adapter.place_order(_buy(), ctx)
    assert again.order is first.order
    assert len(ledger.lots) == 1
    adapter.update_book(_book("99.98", "100", ask_qty="1"))
    adapter.match_resting()
    assert first.order.state == OrderState.FILLED
    assert len(ledger.lots) == 2
    assert ledger.reserved == Decimal("0")
    assert ledger.free == Decimal("4950")
    assert ledger.free + ledger.reserved == ledger.starting - Decimal("50")


def test_cancel_releases_only_the_unfilled_reserve() -> None:
    ledger, adapter = _adapter(maker_fee_pct=Decimal("0"), taker_fee_pct=Decimal("0"))
    book = _book("99.98", "100", ask_qty="0.2")
    adapter.update_book(book)
    ctx = context_from_book(book, now=NOW)
    submission = adapter.place_order(_buy(), ctx)
    assert submission.order is not None
    adapter.cancel_order("buy-1")
    assert submission.order.state == OrderState.CANCELED
    assert ledger.reserved == Decimal("0")
    assert ledger.free == Decimal("4980")
    with pytest.raises(Exception):
        adapter.cancel_order("buy-1")


def test_unknown_reconcile_does_not_resubmit() -> None:
    ledger, adapter = _adapter(maker_fee_pct=Decimal("0"), taker_fee_pct=Decimal("0"))
    book = _book("99.90", "100.20")
    adapter.update_book(book)
    ctx = context_from_book(book, now=NOW)
    submission = adapter.place_order(_buy(limit="100"), ctx)
    assert submission.order is not None
    assert submission.order.state == OrderState.OPEN
    submission.order.state = OrderState.UNKNOWN
    adapter.reconcile_unknown("buy-1")
    assert submission.order.state == OrderState.OPEN
    assert submission.order.state != OrderState.SUBMITTED
    assert ledger.reserved == Decimal("50")
    submission.order.filled_quote = submission.order.quote_budget
    submission.order.state = OrderState.UNKNOWN
    adapter.reconcile_unknown("buy-1")
    assert submission.order.state == OrderState.FILLED
    assert ledger.reserved == Decimal("50")


def test_restart_does_not_spend_the_same_order_twice() -> None:
    ledger, adapter = _adapter(maker_fee_pct=Decimal("0"), taker_fee_pct=Decimal("0"))
    resting = _book("99.90", "100.20")
    adapter.update_book(resting)
    ctx = context_from_book(resting, now=NOW)
    adapter.place_order(_buy(limit="100"), ctx)
    assert ledger.reserved == Decimal("50")
    engine = make_engine("sqlite://")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        save(session, ledger)
    with factory() as session:
        restored = load(session)
    restarted = PaperAdapter(
        restored,
        RiskManager(restored, policy(per_order_quote=Decimal("50"))),
        maker_fee_pct=Decimal("0"),
        taker_fee_pct=Decimal("0"),
    )
    restarted.update_book(resting)
    second = restarted.place_order(_buy(limit="100"), ctx)
    assert second.decision.reason == "idempotent"
    assert restored.reserved == Decimal("50")
    assert restored.free == Decimal("4950")
    filling = _book("99.98", "100", ask_qty="10")
    restarted.update_book(filling)
    restarted.match_resting()
    assert len(restored.lots) == 1
    assert restored.reserved == Decimal("0")
    third = restarted.place_order(_buy(limit="100"), context_from_book(filling, now=NOW))
    assert third.order is not None
    assert len(restored.lots) == 1


def test_example_step_does_not_enter_and_wider_step_round_trips() -> None:
    tight = _params(
        grid_step_pct=Decimal("0.30"),
        maker_fee_pct=Decimal("0.10"),
        taker_fee_pct=Decimal("0.10"),
        slippage_pct=Decimal("0.05"),
        net_target_pct=Decimal("0.10"),
    )
    ledger, adapter = _adapter(slippage_pct=Decimal("0.05"))
    book = _book("99.98", "100")
    proposal = BaseGridStrategy(tight).sync(adapter, book, context_from_book(book, now=NOW))
    assert proposal.intents == []
    assert "below_net_target" in proposal.skipped
    assert ledger.free == Decimal("5000")

    wide = _params(grid_step_pct=Decimal("1.00"), slippage_pct=Decimal("0"))
    cycle_ledger, cycle = _adapter()
    strategy = BaseGridStrategy(wide)
    high = _book("99.98", "100")
    strategy.sync(cycle, high, context_from_book(high, now=NOW))
    assert cycle_ledger.reserved == Decimal("50")
    assert cycle_ledger.lots == {}
    dipped = _book("98.98", "99")
    strategy.sync(cycle, dipped, context_from_book(dipped, now=NOW))
    assert len(cycle_ledger.lots) == 1
    sell = next(order for order in cycle.orders.values() if order.intent.side == "sell")
    assert sell.state == OrderState.OPEN
    lot = next(iter(cycle_ledger.lots.values()))
    limit = exit_limit_price(
        entry_price=lot.entry_price,
        entry_fee_pct=lot.entry_fee_amount / (lot.entry_price * lot.gross_base_qty) * Decimal("100"),
        exit_fee_pct=Decimal("0.10"),
        slippage_pct=Decimal("0"),
        net_target_pct=Decimal("0.10"),
    )
    recovered = _book(str(limit), str(limit * Decimal("1.0002")))
    strategy.sync(cycle, recovered, context_from_book(recovered, now=NOW))
    assert lot.status == "closed"
    assert cycle_ledger.realized_pnl > 0
    assert cycle_ledger.free + cycle_ledger.reserved == cycle_ledger.starting + cycle_ledger.realized_pnl


def test_ai_pause_blocks_only_the_ai_copy() -> None:
    params = _params()
    book = _book("99.98", "100")
    baseline_ledger, baseline = _adapter()
    ai_ledger, ai = _adapter()
    BaseGridStrategy(params).sync(baseline, book, context_from_book(book, now=NOW))
    AIFilteredGridStrategy(params).sync(
        ai,
        book,
        context_from_book(book, now=NOW, regime=RiskRegime.PAUSE_BUYS),
    )
    assert baseline_ledger.reserved == Decimal("50")
    assert ai_ledger.free == Decimal("5000")
    assert ai_ledger.reserved == Decimal("0")
    live = LiveAdapter()
    with pytest.raises(LiveTradingDisabledError):
        live.place_order()


def test_one_ledger_cannot_fund_two_paper_buys() -> None:
    ledger, adapter = _adapter()
    small = open_ledger(balance="60")
    adapter = PaperAdapter(small, RiskManager(small, policy(per_order_quote=Decimal("50"), max_per_asset_exposure_pct=Decimal("200"), max_portfolio_exposure_pct=Decimal("200"))))
    book = _book("99.90", "100.20")
    adapter.update_book(book)
    ctx = context_from_book(book, now=NOW)
    first = adapter.place_order(_buy(limit="100", client_order_id="buy-a"), ctx)
    second = adapter.place_order(_buy(limit="100", client_order_id="buy-b"), ctx)
    assert first.decision.allowed is True
    assert second.decision.allowed is False
    assert second.decision.reason == "out_of_funds"
    assert small.reserved == Decimal("50")
    assert ledger is not small
