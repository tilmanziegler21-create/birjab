"""Instance lock, SQLite backup/restore, and the loopback health probe."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sqlalchemy import text

from app.db.repository import save
from app.db.session import init_db, make_engine, make_session_factory
from app.ops.backup import backup_sqlite, sqlite_paths, verify_backup
from app.ops.lock import InstanceLock, instance_lock_path
from tests.conftest import open_ledger


def test_instance_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "paper.lock"
    first, second = InstanceLock(path), InstanceLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.unlock()
    assert second.acquire() is True
    second.unlock()


def test_instance_lock_path_is_stable_per_database_url() -> None:
    url = "sqlite:///./var/local.db"
    assert instance_lock_path(url) == instance_lock_path(url)
    assert instance_lock_path(url) != instance_lock_path("sqlite:///./var/other.db")


def test_sqlite_backup_replays_to_the_same_snapshot(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    shared = data / "local.db"
    url = f"sqlite:///{shared}"
    for name in ("local.db", "local.baseline.db", "local.ai.db"):
        path = data / name
        engine = make_engine(f"sqlite:///{path}")
        init_db(engine)
        ledger = open_ledger()
        ledger.profit_reserve_pct = Decimal("50")
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
        ledger.apply_sell_fill(
            fill_id="s1",
            lot_id="lot-1",
            price=Decimal("100.3"),
            quantity=Decimal("1"),
            fee_amount=Decimal("0.1003"),
            fee_currency="USDC",
        )
        with make_session_factory(engine)() as session:
            save(session, ledger)
    assert [p.name for p in sqlite_paths(url)] == ["local.db", "local.baseline.db", "local.ai.db"]
    folder = backup_sqlite(url, tmp_path / "backups")
    report = verify_backup(folder)
    assert set(report) == {"shared", "baseline", "ai"}
    assert "reserve=" in report["baseline"]
    assert Decimal("0") < _reserve_from(report["baseline"])


def _reserve_from(line: str) -> Decimal:
    for part in line.split():
        if part.startswith("reserve="):
            return Decimal(part.split("=", 1)[1])
    raise AssertionError(line)


def test_init_db_adds_profit_reserve_to_an_old_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE balance_snapshots ("
                "id INTEGER PRIMARY KEY, quote_currency VARCHAR(8) NOT NULL, "
                "free NUMERIC, reserved NUMERIC, starting NUMERIC, realized_pnl NUMERIC, "
                "peak_equity NUMERIC, trading_blocked BOOLEAN, block_reason VARCHAR(200))"
            )
        )
    init_db(engine)
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(balance_snapshots)"))}
    assert "profit_reserve" in columns


def test_healthcheck_refuses_a_public_url() -> None:
    from app.ops.health import check

    with pytest.raises(SystemExit, match="non-loopback"):
        check("http://8.8.8.8/health", max_lag_sec=60)
