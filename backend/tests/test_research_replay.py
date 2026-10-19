"""Isolated BUY→SELL demo and grid-step comparison. Never opens var/local*.db."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.config import load_settings
from app.market.history import CandleStore
from app.market.public import Candle
from app.research.backtest import compare_steps, compare_window
from app.research.replay import memory_engines, replay_candles

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = Path(__file__).resolve().parents[2] / "configs/paper.binance_public.usdt.yaml"


def _candle(index: int, low: str, high: str, close: str) -> Candle:
    return Candle(
        open_time=NOW + timedelta(minutes=index),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        open_price=Decimal(close),
        interval="1m",
    )


def _settings():
    return load_settings(CONFIG).model_copy(
        update={"exchange": "simulated", "watchlist": ["BTC"], "max_active_pairs": 1, "starting_virtual_balance": Decimal("1000")}
    )


def test_isolated_dip_buy_then_bounce_sell_persists_only_in_memory() -> None:
    settings = _settings()
    # Anchor at first close 100. Step 0.50% → buy 99.50. Conservative: sell phase then buy.
    # Bar 0: establish anchor (no fill). Bar 1: low 99.20 fills buy. Later bars bounce above exit.
    candles = [
        _candle(0, "99.90", "100.10", "100.00"),
        _candle(1, "99.20", "99.40", "99.30"),
        _candle(2, "99.25", "99.45", "99.35"),
        _candle(3, "99.80", "100.20", "100.00"),
        _candle(4, "100.10", "101.20", "101.00"),
        _candle(5, "100.80", "101.40", "101.10"),
    ]
    live = [Path("var/local.db"), Path("var/local.baseline.db"), Path("var/local.ai.db")]
    before = {path: path.stat().st_mtime_ns if path.exists() else None for path in live}
    engines = memory_engines()
    result = replay_candles(settings, candles, "BTC/USDT", step_pct=Decimal("0.50"), engines=engines)
    assert result.buys >= 1
    assert result.sells >= 1
    assert result.completed_cycles >= 1
    assert result.realized_pnl > 0
    assert result.equity > Decimal("1000") or result.realized_pnl > 0
    assert sum(result.fees.values()) > 0
    assert result.drawdown_pct >= 0
    for path, mtime in before.items():
        if mtime is None:
            assert not path.exists()
        else:
            assert path.stat().st_mtime_ns == mtime


def test_compare_steps_splits_train_and_test(tmp_path: Path) -> None:
    settings = _settings()
    store = CandleStore(tmp_path / "h.db")
    series = []
    price = Decimal("100")
    for i in range(80):
        # Gentle 0.6% dips every 4 bars so 0.50% grid can trade; 0.30% even more.
        if i % 4 == 1:
            low = price * Decimal("0.993")
            high = price
            close = price * Decimal("0.997")
        elif i % 4 == 2:
            low = price
            high = price * Decimal("1.012")
            close = price * Decimal("1.008")
            price = close
        else:
            low = price * Decimal("0.999")
            high = price * Decimal("1.001")
            close = price
        series.append(_candle(i, format(low, "f"), format(high, "f"), format(close, "f")))
    store.upsert("BTC/USDT", "1m", series)
    report = compare_steps(settings, store, "BTC/USDT", "1m", start=series[0].open_time, end=series[-1].open_time + timedelta(minutes=1))
    assert report["ok"] is True
    assert report["regime"]["forecast"] is False
    periods = {row["period"] for row in report["runs"]}
    assert "train" in periods and "test" in periods
    steps = {row["step_pct"] for row in report["runs"] if row["period"] == "test"}
    assert {"0.30", "0.50", "1.00"} <= steps
    window = compare_window(
        settings,
        store,
        ["BTC/USDT"],
        "1m",
        start=series[0].open_time,
        end=series[-1].open_time + timedelta(minutes=1),
    )
    assert window["ok"] is True
    assert {row["step_pct"] for row in window["runs"]} == {"0.30", "0.50", "1.00"}
    store.close()
