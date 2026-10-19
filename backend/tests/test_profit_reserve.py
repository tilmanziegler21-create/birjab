"""Profit Reserve: a share of realized profit leaves the buy budget and survives replay."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.accounting.ledger import replay
from app.db.repository import load, save
from app.db.session import init_db, make_engine, make_session_factory
from tests.conftest import open_ledger


def _round_trip(ledger):  # type: ignore[no-untyped-def]
    ledger.reserve("r1", "BTC/USDC", Decimal("100.1"))
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.1"),
        fee_currency="USDC",
    )
    return ledger.apply_sell_fill(
        fill_id="s1",
        lot_id="lot-1",
        price=Decimal("100.3"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.1003"),
        fee_currency="USDC",
    )


def test_default_zero_percent_does_not_sweep() -> None:
    ledger = open_ledger()
    realized = _round_trip(ledger)
    assert realized == Decimal("0.0997")
    assert ledger.profit_reserve == Decimal("0")
    assert ledger.free == Decimal("5000.0997")
    assert ledger.free + ledger.reserved + ledger.profit_reserve == ledger.starting + ledger.quote_in - ledger.quote_out


def test_half_of_realized_profit_leaves_the_buy_budget() -> None:
    ledger = open_ledger()
    ledger.profit_reserve_pct = Decimal("50")
    realized = _round_trip(ledger)
    swept = (realized * Decimal("50") / Decimal("100")).quantize(Decimal("0.00000001"))
    assert ledger.profit_reserve == swept
    assert ledger.free == Decimal("5000.0997") - swept
    assert ledger.reserved == Decimal("0")
    assert ledger.free + ledger.reserved + ledger.profit_reserve == ledger.starting + ledger.quote_in - ledger.quote_out
    marked = ledger.equity({}, Decimal("0.10"))
    assert marked.profit_reserve == swept
    assert marked.realized_pnl == realized
    assert marked.equity == Decimal("5000.0997")
    assert marked.cash == ledger.free + ledger.profit_reserve
    assert "PROFIT_RESERVE" in {event.event_type for event in ledger.events}


def test_a_losing_sell_does_not_sweep() -> None:
    ledger = open_ledger()
    ledger.profit_reserve_pct = Decimal("100")
    ledger.reserve("r1", "BTC/USDC", Decimal("100"))
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    realized = ledger.apply_sell_fill(
        fill_id="s1",
        lot_id="lot-1",
        price=Decimal("90"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    assert realized < 0
    assert ledger.profit_reserve == Decimal("0")
    assert "PROFIT_RESERVE" not in {event.event_type for event in ledger.events}


def test_replay_replays_the_sweep_and_matches_the_snapshot(tmp_path: Path) -> None:
    ledger = open_ledger()
    ledger.profit_reserve_pct = Decimal("50")
    _round_trip(ledger)
    engine = make_engine(f"sqlite:///{tmp_path / 'arm.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        save(session, ledger)
    restored = replay(list(ledger.events))
    assert restored.profit_reserve == ledger.profit_reserve
    assert restored.free == ledger.free
    with factory() as session:
        loaded = load(session)
    assert loaded.trading_blocked is False
    assert loaded.profit_reserve == ledger.profit_reserve
    assert loaded.free == ledger.free
    assert loaded.realized_pnl == ledger.realized_pnl
