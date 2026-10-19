"""Week-3 scenarios from the spec, run through the paper runtime. No network, no live orders."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.ai.analyst import MockAIAnalyst
from app.config import Settings
from app.db.session import init_db, make_engine
from app.main import create_app
from app.market.orderbook import Level, OrderBook
from app.news.events import EventStore
from app.news.ingest import NewsIngestor
from app.news.sources.mock import MockNewsSource, RawItem
from app.paper.feed import ScriptedFeed
from app.paper.runtime import PaperRuntime
from app.risk.emergency import CONFIRM_LIQUIDATE
from app.schemas.orders import OrderIntent, OrderState
from app.schemas.risk import RiskRegime
from tests.conftest import NOW

SYMBOL = "BTC/USDC"

INCIDENT: dict[str, object] = {
    "event_id": "example-incident-001",
    "symbols": ["BTC"],
    "event_type": "network_incident",
    "source_quality": "unverified",
    "severity": "high",
    "sentiment": "negative",
    "novelty": "new",
    "expected_impact_window": "0_4h",
    "hypotheses": ["illustrative hypothesis"],
    "unknowns": ["illustrative unknown"],
    "risk_recommendation": "pause_new_buys",
    "rationale": "illustrative fixture, not a market event",
    "source_urls": ["https://example.org/illustrative-source"],
}

HACK: dict[str, object] = {
    **INCIDENT,
    "event_id": "example-hack-001",
    "event_type": "exchange_ops",
    "source_quality": "official",
    "risk_recommendation": "halt_all",
}


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "exchange": "simulated",
        "watchlist": ["BTC"],
        "grid_step_pct": "1.00",
        "net_target_pct": "0.10",
        "assumed_maker_fee_pct": "0.10",
        "assumed_taker_fee_pct": "0.10",
        "assumed_slippage_pct": "0.05",
        "news_fixtures_path": "",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _book(bid: str, ask: str, now: datetime, *, bid_qty: str = "10", ask_qty: str = "10") -> OrderBook:
    return OrderBook(
        symbol=SYMBOL,
        bids=[Level(Decimal(bid), Decimal(bid_qty))],
        asks=[Level(Decimal(ask), Decimal(ask_qty))],
        updated_at=now,
    )


def _runtime(
    feed: ScriptedFeed,
    *,
    items: list[RawItem] | None = None,
    engines: dict | None = None,
    **overrides: object,
) -> tuple[PaperRuntime, MockNewsSource]:
    settings = _settings(**overrides)
    source = MockNewsSource(items or [])
    news = NewsIngestor(
        [source],
        EventStore(),
        MockAIAnalyst(),
        stale_after_sec=settings.news_stale_after_sec,
        watchlist=list(settings.watchlist),
    )
    return PaperRuntime(settings, feed, news, engines=engines), source


def _fill_one_lot(runtime: PaperRuntime, feed: ScriptedFeed, start: datetime) -> datetime:
    feed.set_book(_book("99.98", "100.00", start))
    runtime.tick(start)
    dip = start + timedelta(seconds=5)
    feed.set_book(_book("98.90", "98.95", dip))
    runtime.tick(dip)
    return dip


def _identity(runtime: PaperRuntime) -> None:
    for arm in runtime.arms.values():
        ledger = arm.ledger
        assert ledger.free + ledger.reserved + ledger.profit_reserve == ledger.starting + ledger.quote_in - ledger.quote_out
        assert ledger.reserved == sum(
            (item.remaining for item in ledger.reservations.values() if item.status == "active"),
            Decimal("0"),
        )


def test_minus_15_pct_in_a_minute_keeps_caps_and_cash_identity() -> None:
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    for arm in runtime.arms.values():
        assert len(arm.ledger.lots) == 1
    price = Decimal("98.95")
    for step in range(1, 13):
        at = at + timedelta(seconds=5)
        price = price * Decimal("0.9865")
        bid = price.quantize(Decimal("0.01"))
        ask = (price * Decimal("1.0004")).quantize(Decimal("0.01"))
        feed.set_book(_book(str(bid), str(ask), at))
        runtime.tick(at)
    assert price < Decimal("98.95") * Decimal("0.85")
    _identity(runtime)
    for arm in runtime.arms.values():
        ledger = arm.ledger
        open_lots = [lot for lot in ledger.lots.values() if lot.remaining_base_qty > 0]
        assert 1 < len(open_lots) <= 6
        marked = ledger.equity(runtime.last_bids, Decimal("0.10"))
        assert marked.equity < Decimal("5000")
        assert marked.gross_position_value <= marked.equity * Decimal("0.15")
        exits = [o for o in arm.adapter.orders.values() if o.intent.side == "sell" and o.state == OrderState.OPEN]
        assert len(exits) == len(open_lots)
    assert runtime.arms["baseline"].ledger.equity(runtime.last_bids, Decimal("0.10")).equity == runtime.arms[
        "ai"
    ].ledger.equity(runtime.last_bids, Decimal("0.10")).equity
    assert runtime.errors == []


def test_drawdown_gate_blocks_new_buys_but_leaves_exits() -> None:
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed, max_drawdown_pct="0.10")
    at = _fill_one_lot(runtime, feed, NOW)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("85.00", "85.04", at))
    before = len(runtime.decision_log)
    runtime.tick(at)
    for arm in runtime.arms.values():
        new = [d for d in runtime.decision_log[before:] if d["arm"] == arm.name]
        rejected = [d for d in new if not d["allowed"]]
        assert rejected
        assert {d["reason"] for d in rejected} == {"max_drawdown"}
        # The buy that was already resting before the crash fills; nothing new is reserved.
        assert len(arm.ledger.lots) == 2
        assert all(d["side"] == "sell" for d in new if d["allowed"])
        exits = [o for o in arm.adapter.orders.values() if o.intent.side == "sell"]
        assert len(exits) == 2 and all(o.state == OrderState.OPEN for o in exits)
        assert arm.ledger.reserved == Decimal("0")
    assert "max_drawdown" in runtime.arm_report("baseline")["reject_reasons"]


def test_false_tweet_pauses_only_ai_and_expires_after_stale_window() -> None:
    feed = ScriptedFeed()
    item = RawItem(
        source="mock",
        url="https://example.org/illustrative-tweet",
        headline="Bitcoin network incident rumour",
        summary="illustrative fixture, not a market event",
        published_at=NOW - timedelta(minutes=1),
        analysis=INCIDENT,
    )
    runtime, _source = _runtime(feed, items=[item])
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    assert runtime.news.regime_for("BTC", NOW) == RiskRegime.PAUSE_BUYS
    assert runtime.arms["baseline"].ledger.reserved == Decimal("50")
    assert runtime.arms["ai"].ledger.reserved == Decimal("0")
    ai_rejects = [d for d in runtime.decision_log if d["arm"] == "ai" and not d["allowed"]]
    assert ai_rejects[0]["reason"] == "news_regime_pause_buys"
    assert "BTC:PAUSE_BUYS" in runtime.snapshot()["paused_reasons"]
    later = NOW + timedelta(seconds=601)
    feed.set_book(_book("99.98", "100.00", later))
    runtime.tick(later)
    assert runtime.news.regime_for("BTC", later) == RiskRegime.NORMAL
    assert runtime.arms["ai"].ledger.reserved == Decimal("50")
    assert runtime.news.analyst_calls == 1
    assert runtime.ab_summary()["ai_vetoes"][0]["baseline_took_it"] is False


def test_exchange_hack_blocks_ai_buys_and_normal_exits_but_confirmed_liquidation_proceeds() -> None:
    feed = ScriptedFeed()
    runtime, source = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    source.items = [
        RawItem(
            source="mock",
            url="https://example.org/illustrative-hack",
            headline="exchange hack affects BTC",
            summary="illustrative fixture, not a market event",
            published_at=at,
            analysis=HACK,
        )
    ]
    at = at + timedelta(seconds=60)
    feed.set_book(_book("98.90", "98.95", at))
    runtime.tick(at)
    assert runtime.news.regime_for("BTC", at) == RiskRegime.HALT_ALL
    ai = runtime.arms["ai"]
    lot = next(iter(ai.ledger.lots.values()))
    ctx = runtime._context(at, feed.fetch(SYMBOL, at), True, Decimal("0"))
    normal_exit = ai.risk.authorize(
        OrderIntent(
            client_order_id="manual-exit",
            symbol=SYMBOL,
            side="sell",
            purpose="exit",
            quantity=Decimal("0.0001"),
            lot_id=lot.lot_id,
            limit_price=Decimal("98.90"),
            reason="test",
            strategy_id="ai_filtered_grid",
        ),
        ctx,
    )
    assert normal_exit.allowed is False
    assert normal_exit.reason == "news_blocks_exit"
    baseline = runtime.arms["baseline"]
    resting_exit = next(o for o in baseline.adapter.orders.values() if o.intent.side == "sell")
    baseline.adapter.cancel_order(resting_exit.client_order_id)
    baseline_exit = baseline.risk.authorize(
        OrderIntent(
            client_order_id="manual-exit-base",
            symbol=SYMBOL,
            side="sell",
            purpose="exit",
            quantity=Decimal("0.0001"),
            lot_id=next(iter(baseline.ledger.lots.values())).lot_id,
            limit_price=Decimal("98.90"),
            reason="test",
            strategy_id="baseline_grid",
        ),
        ctx,
    )
    assert baseline_exit.allowed is True
    baseline.ledger.release_base("manual-exit-base")
    assert runtime.liquidate("wrong") is False
    assert runtime.controls.liquidate_confirmed is False
    assert runtime.liquidate(CONFIRM_LIQUIDATE) is True
    at = at + timedelta(seconds=5)
    feed.set_book(_book("98.90", "98.95", at))
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert all(lot.status == "closed" for lot in arm.ledger.lots.values())
        assert arm.ledger.realized_pnl < 0
        assert arm.ledger.reserved == Decimal("0")
        assert not any(o.intent.side == "buy" and o.state == OrderState.OPEN for o in arm.adapter.orders.values())
        emergency = [o for o in arm.adapter.orders.values() if o.intent.purpose == "emergency_exit"]
        assert emergency and all(o.state == OrderState.FILLED for o in emergency)
    assert "canceled_on_halt" in {d["reason"] for d in runtime.decision_log}
    _identity(runtime)


def test_feed_outage_freezes_both_arms_and_recovers() -> None:
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    before = len(runtime.decision_log)
    feed.outage = True
    for _ in range(3):
        at = at + timedelta(seconds=5)
        report = runtime.tick(at)
        assert report.symbols_failed == {SYMBOL: "feed_outage"}
    assert len(runtime.decision_log) == before
    snapshot = runtime.snapshot()
    assert snapshot["feed"]["symbols"][SYMBOL]["ok"] is False
    assert f"{SYMBOL}:feed:feed_outage" in snapshot["paused_reasons"]
    assert snapshot["arms"]["baseline"]["equity"] is not None
    feed.outage = False
    stale = at + timedelta(seconds=5)
    feed.set_book(_book("97.90", "97.95", stale - timedelta(seconds=16)))
    runtime.tick(stale)
    assert runtime.feed_status[SYMBOL]["stale"] is True
    # A stale book may fill what was already resting, but no new reservation is authorized on it.
    assert {d["reason"] for d in runtime.decision_log[before:] if d["side"] == "buy"} <= {"stale_market_feed"}
    assert all(d["reason"] == "stale_market_feed" for d in runtime.decision_log[before:] if d["side"] == "buy")
    fresh = stale + timedelta(seconds=5)
    feed.set_book(_book("97.90", "97.95", fresh))
    runtime.tick(fresh)
    assert runtime.feed_status[SYMBOL]["stale"] is False
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("50")
    assert runtime.errors == []
    _identity(runtime)


def test_delayed_news_keeps_published_and_first_seen_apart() -> None:
    feed = ScriptedFeed()
    item = RawItem(
        source="mock",
        url="https://example.org/illustrative-late",
        headline="Bitcoin network incident",
        summary="illustrative fixture, not a market event",
        published_at=NOW - timedelta(hours=3),
        analysis=INCIDENT,
    )
    runtime, _source = _runtime(feed, items=[item])
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    view = runtime.news_view(NOW)
    event = view["events"][0]
    assert event["published_at"] == NOW - timedelta(hours=3)
    assert event["first_seen_at"] == NOW
    assert event["weight"] == 1
    assert event["illustrative"] is True
    assert runtime.news.regime_for("BTC", NOW + timedelta(seconds=599)) == RiskRegime.PAUSE_BUYS


def test_depeg_blocks_new_buys_in_both_arms() -> None:
    feed = ScriptedFeed()
    feed.quote_deviation = Decimal("1.50")
    runtime, _source = _runtime(feed)
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    assert {d["reason"] for d in runtime.decision_log} == {"depeg"}
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("0")
    feed.quote_deviation = Decimal("0.20")
    later = NOW + timedelta(seconds=5)
    feed.set_book(_book("99.98", "100.00", later))
    runtime.tick(later)
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("50")


def test_delisting_status_filters_the_pair_and_blocks_entries() -> None:
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("98.90", "98.95", at), status="break")
    before = len(runtime.decision_log)
    runtime.tick(at)
    status = runtime.feed_status[SYMBOL]
    assert status["allowed"] is False
    assert "market_not_trading" in status["reasons"]
    assert runtime.decision_log[before:] == []
    assert f"{SYMBOL}:market_not_trading" in runtime.snapshot()["paused_reasons"]
    at = at + timedelta(seconds=5)
    feed.set_book(_book("97.90", "97.95", at), status="break")
    runtime.tick(at)
    # The resting buy fills on the book, but no new entry or exit is authorized while halted.
    assert {d["reason"] for d in runtime.decision_log[before:]} <= {"market_not_trading", "pair_blocked"}
    assert runtime.decision_log[before:]
    for arm in runtime.arms.values():
        assert len(arm.ledger.lots) == 2
        assert arm.ledger.reserved == Decimal("0")


def test_partial_fill_on_close_leaves_the_remainder_held() -> None:
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    arm = runtime.arms["baseline"]
    lot = next(iter(arm.ledger.lots.values()))
    sell = next(o for o in arm.adapter.orders.values() if o.intent.side == "sell")
    assert sell.state == OrderState.OPEN
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at, bid_qty="0.2"))
    runtime.tick(at)
    assert sell.state == OrderState.PARTIAL
    assert lot.remaining_base_qty == lot.sellable_base_qty - Decimal("0.2")
    assert lot.reserved_base_qty == lot.remaining_base_qty
    _identity(runtime)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    runtime.tick(at)
    assert sell.state == OrderState.FILLED
    assert lot.status == "closed"
    assert arm.ledger.realized_pnl > 0
    assert arm.ledger.free + arm.ledger.reserved == arm.ledger.starting + arm.ledger.realized_pnl
    assert arm.ledger.reserved == Decimal("50")


def test_pause_halt_and_resume_apply_to_both_arms() -> None:
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    runtime.pause(at)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("97.90", "97.95", at))
    before = len(runtime.decision_log)
    runtime.tick(at)
    new = runtime.decision_log[before:]
    assert {d["reason"] for d in new if d["side"] == "buy"} == {"manual_pause_buys"}
    assert all(d["allowed"] for d in new if d["side"] == "sell")
    for arm in runtime.arms.values():
        # The pre-pause resting buy filled; its exit is armed; nothing new is reserved.
        assert len(arm.ledger.lots) == 2
        assert arm.ledger.reserved == Decimal("0")
        exits = [o for o in arm.adapter.orders.values() if o.intent.side == "sell"]
        assert len(exits) == 2 and all(o.state == OrderState.OPEN for o in exits)
    runtime.halt(at)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert all(lot.status == "closed" for lot in arm.ledger.lots.values())
        assert arm.ledger.reserved == Decimal("0")
        assert not any(o.intent.side == "buy" and o.state == OrderState.OPEN for o in arm.adapter.orders.values())
    runtime.resume(at)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("50")
    assert [h["action"] for h in runtime.controls.history] == ["pause_new_buys", "halt_all", "resume"]


def test_restart_reconciles_from_the_database_without_a_second_lot() -> None:
    engines = {name: make_engine("sqlite://") for name in ("shared", "baseline", "ai")}
    for engine in engines.values():
        init_db(engine)
    feed = ScriptedFeed()
    first, _source = _runtime(feed, engines=engines)
    at = _fill_one_lot(first, feed, NOW)
    expected = {name: (arm.ledger.free, arm.ledger.reserved, len(arm.ledger.lots)) for name, arm in first.arms.items()}
    second, _again = _runtime(feed, engines=engines)
    for name, arm in second.arms.items():
        assert (arm.ledger.free, arm.ledger.reserved, len(arm.ledger.lots)) == expected[name]
        assert arm.ledger.trading_blocked is False
        # Resting orders are rebuilt from the database before the first tick, at their saved prices.
        resting_before = {cid: o.limit_price for cid, o in first.arms[name].adapter.orders.items() if o.state == OrderState.OPEN}
        assert {cid: o.limit_price for cid, o in arm.adapter.orders.items()} == resting_before
        assert all(o.state == OrderState.OPEN for o in arm.adapter.orders.values())
    at = at + timedelta(seconds=5)
    feed.set_book(_book("98.90", "98.95", at))
    second.tick(at)
    for name, arm in second.arms.items():
        assert (arm.ledger.free, arm.ledger.reserved, len(arm.ledger.lots)) == expected[name]
        filled = {cid for cid, o in first.arms[name].adapter.orders.items() if o.state == OrderState.FILLED}
        assert set(arm.adapter.orders) == set(first.arms[name].adapter.orders) - filled
        resting_buy = next(o for o in arm.adapter.orders.values() if o.intent.side == "buy")
        assert resting_buy.state == OrderState.OPEN
        assert resting_buy.remaining_quote == Decimal("50")
        rebuilt_exit = next(o for o in arm.adapter.orders.values() if o.intent.side == "sell")
        assert rebuilt_exit.state == OrderState.OPEN
        lot = next(iter(arm.ledger.lots.values()))
        assert rebuilt_exit.remaining_base == lot.reserved_base_qty == lot.remaining_base_qty
    _identity(second)
    # The rebuilt exit still fills when the book reaches it.
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    second.tick(at)
    for arm in second.arms.values():
        assert next(iter(arm.ledger.lots.values())).status == "closed"
        assert arm.ledger.realized_pnl > 0


def test_decision_log_and_equity_history_survive_restart() -> None:
    engines = {name: make_engine("sqlite://") for name in ("shared", "baseline", "ai")}
    for engine in engines.values():
        init_db(engine)
    feed = ScriptedFeed()
    first, _source = _runtime(feed, engines=engines)
    at = _fill_one_lot(first, feed, NOW)
    first.pause(at)
    for _ in range(3):  # buy-2 was pre-authorised and fills; buy-3 is rejected and folds into one line
        at = at + timedelta(seconds=5)
        feed.set_book(_book("97.90", "97.95", at))
        first.tick(at)
    before = [dict(item) for item in first.decision_log]
    assert before and all(item.get("id") is not None for item in before)
    folded = [item for item in before if item["reason"] == "manual_pause_buys"]
    assert len(folded) == 2 and all(item["repeats"] > 1 for item in folded)  # one line per arm, counted
    repeats = folded[0]["repeats"]
    history = first.equity_history()
    assert {len(history["baseline"]), len(history["ai"])} == {first.tick_count}
    assert history["baseline"][-1]["equity"] == first.arms["baseline"].ledger.equity(first.last_bids, Decimal("0.10")).equity

    second, _again = _runtime(feed, engines=engines)
    assert [(i["arm"], i["client_order_id"], i["reason"], i["repeats"], i["id"]) for i in second.decision_log] == [
        (i["arm"], i["client_order_id"], i["reason"], i["repeats"], i["id"]) for i in before
    ]
    assert second.arm_report("baseline")["entries_allowed"] == first.arm_report("baseline")["entries_allowed"]
    second.pause(at)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("97.90", "97.95", at))
    second.tick(at)
    # The counter keeps growing on the restored line instead of starting a new one.
    folded_after = [item for item in second.decision_log if item["reason"] == "manual_pause_buys"]
    assert len(folded_after) == 2 and all(item["repeats"] > repeats for item in folded_after)
    third, _ = _runtime(feed, engines=engines)
    assert [i["repeats"] for i in third.decision_log if i["reason"] == "manual_pause_buys"] == [
        i["repeats"] for i in folded_after
    ]
    assert len(third.equity_history()["ai"]) == first.tick_count + 1
    app = create_app(second.settings, runtime=second)
    rows = TestClient(app).get("/api/equity?limit=2").json()
    assert len(rows["baseline"]) == 2 and isinstance(rows["baseline"][-1]["equity"], str)


def test_frozen_public_book_is_stale_by_update_id_and_recovers() -> None:
    from app.market.public import PublicBookClient
    from app.paper.feed import PublicFeed

    state = {"id": 1000}

    def transport(url: str) -> bytes:
        if "exchangeInfo" in url:
            return json.dumps(
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDC",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
                                {"filterType": "NOTIONAL", "minNotional": "5"},
                            ],
                        }
                    ]
                }
            ).encode()
        if "bookTicker" in url:  # USDC/USDT peg proxy
            return json.dumps({"bidPrice": "0.9999", "bidQty": "1", "askPrice": "1.0001", "askQty": "1"}).encode()
        return json.dumps({"lastUpdateId": state["id"], "bids": [["99.98", "10"]], "asks": [["100.00", "10"]]}).encode()

    feed = PublicFeed(PublicBookClient(transport))
    runtime, _source = _runtime(feed, exchange="binance_public")
    runtime.tick(NOW)
    assert runtime.feed_status[SYMBOL]["stale"] is False
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("50")
    at = NOW + timedelta(seconds=20)
    runtime.tick(at)
    status = runtime.feed_status[SYMBOL]
    assert status["stale"] is True and status["updated_at"] == NOW
    assert runtime.tick_count == 2 and runtime.errors == []
    # A new update id is a live book again.
    state["id"] = 1001
    at = at + timedelta(seconds=5)
    runtime.tick(at)
    assert runtime.feed_status[SYMBOL]["stale"] is False
    assert runtime.feed_status[SYMBOL]["updated_at"] == at
    # And the stale window actually blocks a buy that would otherwise be reserved.
    runtime.halt(at)
    runtime.tick(at + timedelta(seconds=5))
    runtime.resume(at)
    at = at + timedelta(seconds=10 + 16)
    runtime.tick(at)  # same id 1001 since `at - 16s` -> stale
    rejected = [d for d in runtime.decision_log if d["reason"] == "stale_market_feed"]
    assert {d["client_order_id"] for d in rejected} == {"baseline_grid-BTC-buy-1-r1", "ai_filtered_grid-BTC-buy-1-r1"}
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("0")
    state["id"] = 1002
    at = at + timedelta(seconds=5)
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("50")


def test_halt_then_resume_re_enters_under_a_fresh_id_also_after_restart() -> None:
    engines = {name: make_engine("sqlite://") for name in ("shared", "baseline", "ai")}
    for engine in engines.values():
        init_db(engine)
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed, engines=engines)
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    runtime.halt(NOW)
    at = NOW + timedelta(seconds=5)
    feed.set_book(_book("99.98", "100.00", at))
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("0")
        assert arm.adapter.orders[f"{arm.strategy.params.strategy_id}-BTC-buy-1"].state == OrderState.CANCELED
    runtime.resume(at)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.98", "100.00", at))
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("50")
        fresh = arm.adapter.orders[f"{arm.strategy.params.strategy_id}-BTC-buy-1-r1"]
        assert fresh.state == OrderState.OPEN
    _identity(runtime)
    # Halt again, restart from the database: the released reservation id is not reused either.
    runtime.halt(at)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.98", "100.00", at))
    runtime.tick(at)
    second, _again = _runtime(feed, engines=engines)
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.98", "100.00", at))
    second.tick(at)
    for arm in second.arms.values():
        assert arm.ledger.reserved == Decimal("50")
        assert set(arm.adapter.orders) == {f"{arm.strategy.params.strategy_id}-BTC-buy-1-r2"}
        assert {r.status for rid, r in arm.ledger.reservations.items() if rid.endswith(("buy-1", "buy-1-r1"))} == {"released"}
    _identity(second)
    # The fresh entry fills and its exit is armed like any other lot.
    at = at + timedelta(seconds=5)
    feed.set_book(_book("98.90", "98.95", at))
    second.tick(at)
    for arm in second.arms.values():
        assert len(arm.ledger.lots) == 1
        assert any(o.intent.side == "sell" and o.state == OrderState.OPEN for o in arm.adapter.orders.values())


def test_snapshot_is_json_without_secrets_and_api_refuses_unconfirmed_liquidation() -> None:
    feed = ScriptedFeed()
    runtime, _source = _runtime(feed, llm_api_key="local-test-key", exchange_api_key="local-test-key")
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    text = json.dumps(runtime.snapshot())
    assert "local-test-key" not in text
    assert "api_key" not in text.lower()
    app = create_app(runtime.settings, runtime=runtime)
    client = TestClient(app)
    assert client.get("/health").json()["phase"] == 6
    assert client.post("/api/controls/liquidate", json={"confirmation": "yes"}).status_code == 400
    assert client.post("/api/controls/liquidate", json={"confirmation": "x", "extra": 1}).status_code == 422
    assert runtime.controls.liquidate_confirmed is False
    state = client.get("/api/state").json()
    assert state["live_trading"] is False
    assert state["arms"]["baseline"]["reserved_quote"] == "50"
    paths = app.openapi()["paths"]
    assert all("order" not in path for path in paths)
    tick = client.post("/api/tick")
    assert tick.status_code == 200
    assert runtime.tick_count == 2
    with pytest.raises(ValueError):
        _settings(exchange="binance_live")
