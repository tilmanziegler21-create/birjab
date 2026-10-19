"""Compare grid steps on a candle series. Does not touch the live PaperRuntime or its DB."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.market.features import atr_pct, classify_regime, dip_frequency
from app.market.history import CandleStore
from app.research.replay import ReplayResult, memory_engines, replay_candles

STEPS = (Decimal("0.30"), Decimal("0.50"), Decimal("1.00"))


def split_range(start: datetime, end: datetime, train_frac: Decimal = Decimal("0.70")) -> tuple[datetime, datetime, datetime]:
    span = (end - start).total_seconds()
    cut = start.timestamp() + float(span) * float(train_frac)
    mid = datetime.fromtimestamp(cut, tz=start.tzinfo)
    return start, mid, end


def compare_steps(
    settings: Settings,
    store: CandleStore,
    symbol: str,
    interval: str,
    *,
    start: datetime,
    end: datetime,
    train_frac: Decimal = Decimal("0.70"),
) -> dict[str, Any]:
    candles = store.load(symbol, interval, start=start, end=end)
    if len(candles) < 20:
        return {"ok": False, "reason": "not_enough_candles", "count": len(candles)}
    _train_start, cut, _end = split_range(candles[0].open_time, candles[-1].open_time, train_frac)
    train = [c for c in candles if c.open_time < cut]
    test = [c for c in candles if c.open_time >= cut]
    regime = classify_regime(candles)
    dips = dip_frequency(candles)
    try:
        atr = atr_pct(candles)
    except ValueError:
        atr = None
    adaptive = None
    if atr is not None:
        adaptive = min(max(atr, Decimal("0.30")), Decimal("1.00")).quantize(Decimal("0.01"))
    rows: list[dict[str, Any]] = []
    for period_name, series in (("train", train), ("test", test)):
        if len(series) < 10:
            continue
        for step in STEPS:
            result = replay_candles(settings, series, symbol, step_pct=step, engines=memory_engines())
            rows.append({"period": period_name, **result.as_dict()})
        if adaptive is not None:
            result = replay_candles(settings, series, symbol, adaptive_step=adaptive, engines=memory_engines())
            rows.append({"period": period_name, **result.as_dict()})
    return {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "bars": len(candles),
        "train_bars": len(train),
        "test_bars": len(test),
        "split_at": cut.isoformat(),
        "regime": regime,
        "dip_frequency_pct_of_bars": {k: str(v) for k, v in dips.items()},
        "atr_pct": str(atr) if atr is not None else None,
        "adaptive_step_pct": str(adaptive) if adaptive is not None else None,
        "disclaimer": "Indicators and replay equity are research, not a return forecast. Live paper params were not changed.",
        "runs": rows,
    }


def compare_window(
    settings: Settings,
    store: CandleStore,
    symbols: list[str],
    interval: str,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Full-window replay per symbol and step. Isolated memory engines only."""

    runs: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for symbol in symbols:
        candles = store.load(symbol, interval, start=start, end=end)
        coverage.append({"symbol": symbol, "bars": len(candles)})
        if len(candles) < 20:
            continue
        for step in STEPS:
            result = replay_candles(settings, candles, symbol, step_pct=step, engines=memory_engines())
            runs.append({"symbol": symbol, "period": "window", **result.as_dict()})
    return {
        "ok": True,
        "interval": interval,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "coverage": coverage,
        "runs": runs,
        "disclaimer": "Conservative candle replay, not a live paper result. Live strategy was not changed.",
    }
