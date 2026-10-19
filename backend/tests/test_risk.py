from __future__ import annotations

from decimal import Decimal

import pytest

from app.risk.emergency import CONFIRM_LIQUIDATE, EmergencyControl
from app.risk.manager import RiskManager
from app.schemas.orders import OrderIntent
from app.schemas.risk import RiskRegime
from tests.conftest import NOW, aged, buy_intent, context, open_ledger, policy


def test_reserve_is_idempotent_and_rejects_oversized_orders() -> None:
    ledger = open_ledger()
    manager = RiskManager(ledger, policy())
    market = context()
    first = manager.authorize(buy_intent(quote_value="50"), market)
    second = manager.authorize(buy_intent(quote_value="80"), market)
    assert first.allowed is True
    assert second.allowed is True
    assert second.reason == "idempotent"
    assert ledger.free == Decimal("4950")
    assert ledger.reserved == Decimal("50")
    oversized = manager.authorize(buy_intent("buy-2", quote_value="50.01"), market)
    assert oversized.allowed is False
    assert oversized.reason == "order_exceeds_per_order_quote"
    assert ledger.free == Decimal("4950")


def test_two_strategies_share_one_cash_balance() -> None:
    ledger = open_ledger(balance="80")
    manager = RiskManager(
        ledger,
        policy(per_order_quote=Decimal("50"), max_per_asset_exposure_pct=Decimal("200"), max_portfolio_exposure_pct=Decimal("200")),
    )
    market = context()
    baseline = manager.authorize(buy_intent("base-1", strategy_id="baseline_grid"), market)
    ai = manager.authorize(buy_intent("ai-1", strategy_id="ai_filtered_grid"), market)
    assert baseline.allowed is True
    assert ai.allowed is False
    assert ai.reason == "out_of_funds"
    assert ledger.free == Decimal("30")
    assert ledger.reserved == Decimal("50")


def test_exposure_lot_and_pair_caps() -> None:
    ledger = open_ledger()
    manager = RiskManager(ledger, policy(per_order_quote=Decimal("1000")))
    market = context()
    assert manager.authorize(buy_intent("a", quote_value="700"), market).allowed is True
    exact = manager.authorize(buy_intent("b", quote_value="50"), market)
    assert exact.allowed is True
    assert ledger.reserved == Decimal("750")
    overflow = manager.authorize(buy_intent("c", quote_value="1"), market)
    assert overflow.reason == "per_asset_exposure"

    wide = RiskManager(
        open_ledger(),
        policy(per_order_quote=Decimal("1000"), max_per_asset_exposure_pct=Decimal("100"), max_active_pairs=3),
    )
    market = context()
    for index, symbol in enumerate(("BTC/USDC", "ETH/USDC")):
        decision = wide.authorize(buy_intent(f"p{index}", symbol, "1000"), market)
        assert decision.allowed is True
    third = wide.authorize(buy_intent("p2", "SOL/USDC", "50"), market)
    assert third.reason == "portfolio_exposure"

    pairs = RiskManager(open_ledger(), policy())
    market = context()
    for index, symbol in enumerate(("BTC/USDC", "ETH/USDC", "SOL/USDC")):
        assert pairs.authorize(buy_intent(f"s{index}", symbol, "50"), market).allowed is True
    blocked = pairs.authorize(buy_intent("s3", "XRP/USDC", "50"), market)
    assert blocked.reason == "max_active_pairs"

    lots = RiskManager(open_ledger(), policy())
    market = context()
    for index in range(6):
        assert lots.authorize(buy_intent(f"l{index}", quote_value="50"), market).allowed is True
    seventh = lots.authorize(buy_intent("l6", quote_value="50"), market)
    assert seventh.reason == "max_open_lots"
    assert lots.ledger.reserved == Decimal("300")


def test_feed_depeg_and_drawdown_block_new_buys() -> None:
    ledger = open_ledger()
    manager = RiskManager(ledger, policy())
    stale = manager.authorize(buy_intent(), context(feed_timestamp=aged(NOW, 16)))
    assert stale.reason == "stale_market_feed"
    assert ledger.free == Decimal("5000")

    wide = manager.authorize(buy_intent("spread"), context(spread_pct=Decimal("0.51")))
    assert wide.reason == "spread_too_wide"

    depeg = manager.authorize(buy_intent("depeg"), context(quote_deviation_pct=Decimal("1.01")))
    assert depeg.reason == "depeg"
    unknown = manager.authorize(buy_intent("peg"), context(quote_deviation_pct=None))
    assert unknown.reason == "quote_peg_unknown"

    drawdown = RiskManager(open_ledger(), policy(per_order_quote=Decimal("1000"), max_per_asset_exposure_pct=Decimal("100"), max_portfolio_exposure_pct=Decimal("100")))
    entry = context(bids={"BTC/USDC": Decimal("100")})
    assert drawdown.authorize(buy_intent("d1", quote_value="1000"), entry).allowed is True
    drawdown.ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="d1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("10"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    crashed = context(bids={"BTC/USDC": Decimal("50")})
    blocked = drawdown.authorize(buy_intent("d2", "ETH/USDC", "50"), crashed)
    assert blocked.reason == "max_drawdown"
    assert drawdown.ledger.free == Decimal("4000")
    missing = drawdown.authorize(buy_intent("d3", "ETH/USDC", "50"), context(bids={}))
    assert missing.reason == "missing_mark"


def test_ai_news_filter_does_not_apply_to_baseline() -> None:
    ledger = open_ledger()
    manager = RiskManager(ledger, policy())
    paused = context(regime=RiskRegime.PAUSE_BUYS)
    ai = manager.authorize(buy_intent("ai", strategy_id="ai_filtered_grid"), paused)
    baseline = manager.authorize(buy_intent("base", strategy_id="baseline_grid"), paused)
    assert ai.reason == "news_regime_pause_buys"
    assert baseline.allowed is True

    dark = RiskManager(open_ledger(), policy())
    unavailable = context(news_last_ok_at=None)
    assert dark.authorize(buy_intent("ai2", strategy_id="ai_filtered_grid"), unavailable).reason == "news_unavailable"
    assert dark.authorize(buy_intent("base2"), unavailable).allowed is True

    stale_news = RiskManager(open_ledger(), policy())
    old = context(news_last_ok_at=aged(NOW, 601))
    assert stale_news.authorize(buy_intent("ai3", strategy_id="ai_filtered_grid"), old).reason == "news_stale"
    assert stale_news.authorize(buy_intent("base3"), old).allowed is True

    depeg_news = RiskManager(open_ledger(), policy())
    regime = context(regime=RiskRegime.DEPEG, quote_deviation_pct=Decimal("0"))
    assert depeg_news.authorize(buy_intent("ai4", strategy_id="ai_filtered_grid"), regime).reason == "news_regime_depeg"
    assert depeg_news.authorize(buy_intent("base4"), regime).allowed is True


def test_reduce_size_without_multiplier_does_not_reserve() -> None:
    ledger = open_ledger()
    manager = RiskManager(ledger, policy())
    decision = manager.authorize(
        buy_intent(strategy_id="ai_filtered_grid"),
        context(regime=RiskRegime.CAUTION, size_multiplier=None),
    )
    assert decision.reason == "reduce_size_without_multiplier"
    assert ledger.free == Decimal("5000")
    halved = manager.authorize(
        buy_intent("buy-half", strategy_id="ai_filtered_grid"),
        context(regime=RiskRegime.CAUTION, size_multiplier=Decimal("0.5")),
    )
    assert halved.allowed is True
    assert halved.reserved_quote == Decimal("25")
    assert ledger.reserved == Decimal("25")


def test_pause_allows_exit_and_halt_requires_confirmation() -> None:
    ledger = open_ledger()
    manager = RiskManager(ledger, policy())
    assert manager.authorize(buy_intent(), context()).allowed is True
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="buy-1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("0.5"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    paused = context(manual_pause_buys=True, bids={"BTC/USDC": Decimal("100")})
    assert manager.authorize(buy_intent("buy-2"), paused).reason == "manual_pause_buys"
    sell = OrderIntent(
        client_order_id="sell-1",
        symbol="BTC/USDC",
        side="sell",
        purpose="exit",
        quantity=Decimal("0.5"),
        lot_id="lot-1",
        reason="exit",
        strategy_id="baseline_grid",
    )
    assert manager.authorize(sell, paused).allowed is True
    halted = context(manual_halt=True, bids={"BTC/USDC": Decimal("100")})
    blocked = manager.authorize(
        OrderIntent(
            client_order_id="sell-2",
            symbol="BTC/USDC",
            side="sell",
            purpose="exit",
            quantity=Decimal("0.5"),
            lot_id="lot-1",
            reason="exit",
            strategy_id="baseline_grid",
        ),
        halted,
    )
    assert blocked.reason == "manual_halt"


def test_emergency_liquidate_creates_intents_without_sending_orders() -> None:
    from app.adapters.exchange import LiveAdapter, LiveTradingDisabledError

    ledger = open_ledger()
    ledger.reserve("r1", "BTC/USDC", Decimal("100"))
    lot = ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    control = EmergencyControl()
    assert control.request_liquidate("no", [lot]) == []
    assert control.halt_all is False
    intents = control.request_liquidate(CONFIRM_LIQUIDATE, [lot])
    assert len(intents) == 1
    assert intents[0].purpose == "emergency_exit"
    assert intents[0].quantity == Decimal("1")
    assert ledger.lots["lot-1"].remaining_base_qty == Decimal("1")
    adapter = LiveAdapter(api_key="local-test-key", api_secret="local-test-secret")
    with pytest.raises(LiveTradingDisabledError):
        adapter.place_order(intents[0])
    manager = RiskManager(ledger, policy())
    decision = manager.authorize(
        intents[0],
        context(
            manual_halt=True,
            emergency_confirmed=True,
            bids={"BTC/USDC": Decimal("100")},
        ),
    )
    assert decision.allowed is True
    assert ledger.lots["lot-1"].reserved_base_qty == Decimal("1")


def test_concurrent_risk_authorization_spends_cash_once() -> None:
    from threading import Thread

    ledger = open_ledger(balance="100")
    manager = RiskManager(
        ledger,
        policy(
            per_order_quote=Decimal("80"),
            max_per_asset_exposure_pct=Decimal("200"),
            max_portfolio_exposure_pct=Decimal("200"),
        ),
    )
    market = context()
    results: list[bool] = []

    def worker(index: int) -> None:
        decision = manager.authorize(buy_intent(f"c{index}", quote_value="80"), market)
        results.append(decision.allowed)

    threads = [Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert ledger.free == Decimal("20")
    assert ledger.reserved == Decimal("80")
