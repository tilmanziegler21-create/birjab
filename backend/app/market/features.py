"""Indicators from candles. A candle is not a limit-order fill and not a forecast."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from app.market.public import Candle
from app.money import money

ATR_PERIOD = 14
Regime = Literal["RANGE", "UPTREND", "DOWNTREND", "HIGH_VOLATILITY", "UNKNOWN"]


def atr_pct(candles: list[Candle], *, period: int = ATR_PERIOD) -> Decimal:
    if period < 1:
        raise ValueError("atr period")
    if len(candles) < period + 1:
        raise ValueError("not_enough_candles")
    window = candles[-(period + 1) :]
    ranges: list[Decimal] = []
    for previous, bar in zip(window, window[1:]):
        true_range = max(
            bar.high - bar.low,
            abs(bar.high - previous.close),
            abs(bar.low - previous.close),
        )
        ranges.append(true_range)
    last_close = money(window[-1].close)
    if last_close <= 0:
        raise ValueError("close")
    average = sum(ranges, Decimal("0")) / Decimal(len(ranges))
    return average / last_close * Decimal("100")


def ema(values: list[Decimal], period: int) -> Decimal:
    if period < 1 or len(values) < period:
        raise ValueError("not_enough_values")
    k = Decimal("2") / Decimal(period + 1)
    current = sum(values[:period], Decimal("0")) / Decimal(period)
    for value in values[period:]:
        current = value * k + current * (Decimal("1") - k)
    return current


def rsi(closes: list[Decimal], period: int = 14) -> Decimal:
    if len(closes) < period + 1:
        raise ValueError("not_enough_values")
    gains = Decimal("0")
    losses = Decimal("0")
    window = closes[-(period + 1) :]
    for previous, current in zip(window, window[1:]):
        delta = current - previous
        if delta > 0:
            gains += delta
        else:
            losses += -delta
    if losses == 0:
        return Decimal("100")
    rs = (gains / Decimal(period)) / (losses / Decimal(period))
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def realized_vol_pct(closes: list[Decimal], period: int = 24) -> Decimal:
    if len(closes) < period + 1:
        raise ValueError("not_enough_values")
    window = closes[-(period + 1) :]
    rets: list[Decimal] = []
    for previous, current in zip(window, window[1:]):
        if previous <= 0:
            raise ValueError("close")
        rets.append((current - previous) / previous)
    mean = sum(rets, Decimal("0")) / Decimal(len(rets))
    var = sum(((item - mean) ** 2 for item in rets), Decimal("0")) / Decimal(len(rets))
    # population stdev as percent of price, not annualised — a local scale, not a forecast
    return var.sqrt() * Decimal("100")


def dip_frequency(candles: list[Candle], thresholds: tuple[Decimal, ...] = (Decimal("0.3"), Decimal("0.5"), Decimal("1.0"))) -> dict[str, Decimal]:
    """Share of bars whose (high-low)/close meets each threshold. Not a fill probability."""

    if not candles:
        return {f"{t}%": Decimal("0") for t in thresholds}
    counts = {t: 0 for t in thresholds}
    for bar in candles:
        if bar.close <= 0:
            continue
        span = (bar.high - bar.low) / bar.close * Decimal("100")
        for t in thresholds:
            if span >= t:
                counts[t] += 1
    n = Decimal(len(candles))
    return {f"{t}%": (Decimal(counts[t]) / n * Decimal("100")) for t in thresholds}


def classify_regime(candles: list[Candle]) -> dict[str, object]:
    """Heuristic label from EMA slope, ATR and RSI. Not a prediction."""

    if len(candles) < 50:
        return {"regime": "UNKNOWN", "reason": "not_enough_candles", "forecast": False}
    closes = [c.close for c in candles]
    try:
        fast = ema(closes, 12)
        slow = ema(closes, 26)
        atr = atr_pct(candles)
        vol = realized_vol_pct(closes, period=min(24, len(closes) - 1))
        strength = rsi(closes)
    except ValueError as exc:
        return {"regime": "UNKNOWN", "reason": str(exc), "forecast": False}
    slope = (fast - slow) / slow * Decimal("100") if slow > 0 else Decimal("0")
    if atr >= Decimal("1.50") or vol >= Decimal("1.20"):
        regime: Regime = "HIGH_VOLATILITY"
        reason = f"atr {atr:.2f}% or realized vol {vol:.2f}% above local thresholds"
    elif slope >= Decimal("0.40") and strength >= Decimal("55"):
        regime = "UPTREND"
        reason = f"EMA12-26 {slope:.2f}% and RSI {strength:.1f}"
    elif slope <= Decimal("-0.40") and strength <= Decimal("45"):
        regime = "DOWNTREND"
        reason = f"EMA12-26 {slope:.2f}% and RSI {strength:.1f}"
    else:
        regime = "RANGE"
        reason = f"EMA12-26 {slope:.2f}% ATR {atr:.2f}%"
    return {
        "regime": regime,
        "reason": reason,
        "atr_pct": atr,
        "ema_fast": fast,
        "ema_slow": slow,
        "rsi": strength,
        "realized_vol_pct": vol,
        "forecast": False,
    }
