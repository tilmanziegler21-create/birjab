from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.market.archive import ArchiveError, assert_archive_url, parse_kline_zip
from app.market.features import atr_pct, classify_regime, ema, rsi
from app.market.history import CandleStore
from app.market.public import Candle, PublicBookError, klines_url


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


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


def test_store_dedups_and_reports_gaps(tmp_path: Path) -> None:
    store = CandleStore(tmp_path / "c.db")
    first = [_bar(0, "100"), _bar(2, "102")]
    assert store.upsert("BTC/USDT", "1h", first) == 2
    assert store.upsert("BTC/USDT", "1h", first) == 0
    gaps = store.gaps("BTC/USDT", "1h")
    assert gaps and gaps[0]["missing"] == 1
    store.upsert("BTC/USDT", "1h", [_bar(1, "101")])
    assert store.gaps("BTC/USDT", "1h") == []
    assert len(store.load("BTC/USDT", "1h")) == 3
    store.close()


def test_indicators_and_regime_are_not_a_forecast() -> None:
    up = [_bar(i, str(100 + i)) for i in range(60)]
    classified = classify_regime(up)
    assert classified["forecast"] is False
    assert classified["regime"] in {"UPTREND", "RANGE", "HIGH_VOLATILITY", "UNKNOWN"}
    assert ema([c.close for c in up], 12) > 100
    assert Decimal("0") <= rsi([c.close for c in up]) <= Decimal("100")
    assert atr_pct(up) >= 0


def test_archive_url_allowlist() -> None:
    assert_archive_url("https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-01.zip")
    with pytest.raises(ArchiveError):
        assert_archive_url("https://evil.example/data/spot/x.zip")
    with pytest.raises(PublicBookError):
        klines_url("BTC", "USDT", interval="2h")


def test_parse_kline_zip_roundtrip() -> None:
    import io
    import zipfile

    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as zf:
        zf.writestr(
            "BTCUSDT-1h.csv",
            "open_time,open,high,low,close,volume,close_time,q,n,t,t2,i\n"
            "1711929600000,100,101,99,100.5,12,1711933199999,0,0,0,0,0\n",
        )
    candles = parse_kline_zip(raw.getvalue(), interval="1h")
    assert len(candles) == 1 and candles[0].close == Decimal("100.5") and candles[0].low == Decimal("99")
    raw2 = io.BytesIO()
    with zipfile.ZipFile(raw2, "w") as zf:
        zf.writestr("BTCUSDT-1h.csv", "1711929600000000,100,101,99,100.5,12,0,0,0,0,0,0\n")
    micro = parse_kline_zip(raw2.getvalue(), interval="1h")
    assert micro[0].open_time.year == 2024
