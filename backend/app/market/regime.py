"""Display-only market regime. Never imported by RiskManager, GridStrategy or PaperAdapter."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.market.features import classify_regime
from app.market.history import CandleStore
from app.market.public import Candle, PublicBookClient, PublicBookError

MIN_BARS = 50
WANT_BARS = 120


def default_store() -> Path:
    """Prefer the repo-level Vision store so cwd of the Telegram process does not matter."""

    repo = Path(__file__).resolve().parents[3] / "var" / "market" / "candles.db"
    if repo.is_file():
        return repo
    return Path("var/market/candles.db")


DEFAULT_STORE = default_store()


def diagnose(candles: list[Candle], *, source: str) -> dict[str, Any]:
    classified = classify_regime(candles)
    classified["source"] = source
    classified["bars"] = len(candles)
    classified["forecast"] = False
    classified["used_for_orders"] = False
    if classified.get("regime") == "UNKNOWN" and len(candles) < MIN_BARS:
        classified["reason"] = f"нужно ≥{MIN_BARS} часовых свечей, есть {len(candles)}"
    return classified


def from_store(symbol: str, path: Path | None = None) -> dict[str, Any] | None:
    store_path = Path(path) if path is not None else default_store()
    if not store_path.is_file():
        return None
    store = CandleStore(store_path)
    try:
        candles = store.load(symbol, "1h")[-WANT_BARS:]
    finally:
        store.close()
    if not candles:
        return None
    return diagnose(candles, source=f"local 1h store ({store_path.name})")


def from_public(client: PublicBookClient, base: str, quote: str) -> dict[str, Any]:
    try:
        candles = client.fetch_klines(base, quote, interval="1h", limit=WANT_BARS)
    except PublicBookError as exc:
        return {
            "regime": "UNKNOWN",
            "reason": f"публичные klines недоступны: {exc}",
            "source": "binance_public_klines",
            "bars": 0,
            "forecast": False,
            "used_for_orders": False,
        }
    return diagnose(candles, source="binance_public 1h klines")


def for_symbol(
    symbol: str,
    *,
    client: PublicBookClient | None = None,
    store_path: Path | None = None,
) -> dict[str, Any]:
    """Prefer the local Vision store; fall back to a public GET. Display only."""

    local = from_store(symbol, store_path)
    if local is not None:
        return local
    if client is None:
        return {
            "regime": "UNKNOWN",
            "reason": "нет локальной истории и нет публичного клиента",
            "source": "none",
            "bars": 0,
            "forecast": False,
            "used_for_orders": False,
        }
    base, _, quote = symbol.partition("/")
    return from_public(client, base, quote or "USDT")


def label(payload: object) -> str:
    if payload is None or payload == "":
        return "нет истории"
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return "нет истории"
    regime = str(payload.get("regime") or "UNKNOWN")
    bars = payload.get("bars")
    extra = f", {bars}×1h" if bars else ""
    return f"{regime}{extra} · не для ордеров"
