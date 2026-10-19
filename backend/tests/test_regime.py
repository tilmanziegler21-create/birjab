"""Display-only regime classifier. Must not be wired into orders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.market.features import classify_regime
from app.market.history import CandleStore
from app.market.public import Candle
from app.market.regime import diagnose, for_symbol, label

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _bar(index: int, close: str, *, high: str | None = None, low: str | None = None) -> Candle:
    price = Decimal(close)
    return Candle(
        open_time=NOW + timedelta(hours=index),
        high=Decimal(high) if high else price,
        low=Decimal(low) if low else price,
        close=price,
        volume=Decimal("1"),
        open_price=price,
        interval="1h",
    )


def test_unknown_when_cache_only_has_24_hours() -> None:
    short = [_bar(i, "100") for i in range(24)]
    classified = classify_regime(short)
    assert classified["regime"] == "UNKNOWN"
    assert classified["forecast"] is False
    diagnosed = diagnose(short, source="test")
    assert diagnosed["used_for_orders"] is False
    assert "50" in str(diagnosed["reason"])


def test_classifier_separates_four_regimes() -> None:
    up = [_bar(i, str(100 + i)) for i in range(80)]
    down = [_bar(i, str(180 - i)) for i in range(80)]
    flat = [_bar(i, "100.00" if i % 2 == 0 else "100.05") for i in range(80)]
    wild = [_bar(i, "100", high="104", low="96") for i in range(80)]
    assert classify_regime(up)["regime"] == "UPTREND"
    assert classify_regime(down)["regime"] == "DOWNTREND"
    assert classify_regime(flat)["regime"] == "RANGE"
    assert classify_regime(wild)["regime"] == "HIGH_VOLATILITY"


def test_for_symbol_reads_local_store_and_stays_display_only(tmp_path: Path) -> None:
    path = tmp_path / "candles.db"
    store = CandleStore(path)
    store.upsert("BTC/USDT", "1h", [_bar(i, str(100 + i * Decimal("0.2"))) for i in range(120)])
    store.close()
    classified = for_symbol("BTC/USDT", store_path=path)
    assert classified["bars"] == 120
    assert classified["forecast"] is False
    assert classified["used_for_orders"] is False
    assert classified["regime"] in {"RANGE", "UPTREND", "DOWNTREND", "HIGH_VOLATILITY"}
    assert "не для ордеров" in label(classified)


def test_trading_path_does_not_import_regime_module() -> None:
    files = [
        ROOT / "app/risk/manager.py",
        ROOT / "app/strategy/grid.py",
        ROOT / "app/adapters/paper.py",
        ROOT / "app/paper/runtime.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "market.regime" not in text
        assert "from app.market.features import classify_regime" not in text
