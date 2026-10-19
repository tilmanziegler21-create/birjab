from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.accounting.ledger import LedgerHalted
from app.db.models import LedgerEventRow
from app.db.models import BalanceSnapshotRow
from app.db.repository import load, repair_from_replay, save
from app.db.session import init_db, make_engine, make_session_factory
from tests.conftest import open_ledger


def _factory():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)


def test_restart_replays_fills_and_continues() -> None:
    ledger = open_ledger(quote="EUR", balance="1000")
    ledger.reserve("r1", "ETH/EUR", Decimal("100"))
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="ETH/EUR",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.001"),
        fee_currency="ETH",
    )
    factory = _factory()
    with factory() as session:
        save(session, ledger)
    with factory() as session:
        loaded = load(session)
    assert loaded.trading_blocked is False
    assert loaded.quote_currency is not None
    assert loaded.quote_currency.value == "EUR"
    assert loaded.free == Decimal("900")
    assert loaded.lots["lot-1"].sellable_base_qty == Decimal("0.999")
    loaded.reserve("r2", "ETH/EUR", Decimal("10"))
    with factory() as session:
        save(session, loaded)
        count = session.scalar(select(func.count()).select_from(LedgerEventRow))
    assert count == 4
    with factory() as session:
        again = load(session)
    assert again.free == Decimal("890")
    assert again.reserved == Decimal("10")


def test_snapshot_tamper_blocks_trading_until_replay_repair() -> None:
    ledger = open_ledger()
    ledger.reserve("r1", "BTC/USDC", Decimal("50"))
    factory = _factory()
    with factory() as session:
        save(session, ledger)
    with factory() as session:
        snapshot = session.get(BalanceSnapshotRow, 1)
        assert snapshot is not None
        snapshot.free = Decimal("1")
        session.commit()
    with factory() as session:
        loaded = load(session)
        assert loaded.trading_blocked is True
        assert loaded.block_reason == "balance_snapshot_mismatch"
        assert loaded.free == Decimal("4950")
        with pytest.raises(LedgerHalted):
            loaded.reserve("r2", "ETH/USDC", Decimal("10"))
    with factory() as session:
        still = load(session)
        assert still.trading_blocked is True
        repair_from_replay(session, still)
    with factory() as session:
        repaired = load(session)
    assert repaired.trading_blocked is False
    assert repaired.free == Decimal("4950")
    repaired.reserve("r2", "ETH/USDC", Decimal("10"))
    assert repaired.free == Decimal("4940")
