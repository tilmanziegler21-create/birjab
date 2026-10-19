"""Local candle store. Separate from the paper ledgers. Dedup is on (symbol, interval, open_time)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.market.public import Candle
from app.money import money

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000, "1d": 86_400_000}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class CandleStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                open_time_ms INTEGER NOT NULL,
                open TEXT,
                high TEXT NOT NULL,
                low TEXT NOT NULL,
                close TEXT NOT NULL,
                volume TEXT NOT NULL,
                PRIMARY KEY (symbol, interval, open_time_ms)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, symbol: str, interval: str, candles: list[Candle]) -> int:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval {interval}")
        added = 0
        for candle in candles:
            opened = _aware(candle.open_time)
            ms = int(opened.timestamp() * 1000)
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO candles
                (symbol, interval, open_time_ms, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    interval,
                    ms,
                    format(candle.open_price, "f") if candle.open_price is not None else None,
                    format(candle.high, "f"),
                    format(candle.low, "f"),
                    format(candle.close, "f"),
                    format(candle.volume, "f"),
                ),
            )
            added += cur.rowcount
        self._conn.commit()
        return added

    def load(self, symbol: str, interval: str, *, start: datetime | None = None, end: datetime | None = None) -> list[Candle]:
        query = "SELECT open_time_ms, open, high, low, close, volume FROM candles WHERE symbol=? AND interval=?"
        params: list[object] = [symbol, interval]
        if start is not None:
            query += " AND open_time_ms>=?"
            params.append(int(_aware(start).timestamp() * 1000))
        if end is not None:
            query += " AND open_time_ms<?"
            params.append(int(_aware(end).timestamp() * 1000))
        query += " ORDER BY open_time_ms"
        rows = self._conn.execute(query, params).fetchall()
        out: list[Candle] = []
        for ms, open_s, high, low, close, volume in rows:
            out.append(
                Candle(
                    open_time=datetime.fromtimestamp(ms / 1000, tz=timezone.utc),
                    high=money(high),
                    low=money(low),
                    close=money(close),
                    volume=money(volume),
                    open_price=money(open_s) if open_s is not None else None,
                    interval=interval,
                )
            )
        return out

    def coverage(self, symbol: str, interval: str) -> dict[str, object]:
        row = self._conn.execute(
            "SELECT COUNT(*), MIN(open_time_ms), MAX(open_time_ms) FROM candles WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
        count, first, last = row
        gaps = self.gaps(symbol, interval)
        return {
            "symbol": symbol,
            "interval": interval,
            "count": int(count or 0),
            "first": datetime.fromtimestamp(first / 1000, tz=timezone.utc).isoformat() if first else None,
            "last": datetime.fromtimestamp(last / 1000, tz=timezone.utc).isoformat() if last else None,
            "gaps": len(gaps),
            "gap_bars": sum(g["missing"] for g in gaps),
        }

    def gaps(self, symbol: str, interval: str) -> list[dict[str, object]]:
        step = INTERVAL_MS[interval]
        rows = self._conn.execute(
            "SELECT open_time_ms FROM candles WHERE symbol=? AND interval=? ORDER BY open_time_ms",
            (symbol, interval),
        ).fetchall()
        found: list[dict[str, object]] = []
        previous = None
        for (ms,) in rows:
            if previous is not None and ms - previous > step:
                missing = (ms - previous) // step - 1
                found.append(
                    {
                        "after": datetime.fromtimestamp(previous / 1000, tz=timezone.utc).isoformat(),
                        "before": datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(),
                        "missing": int(missing),
                    }
                )
            previous = ms
        return found

    def expected_count(self, interval: str, start: datetime, end: datetime) -> int:
        step = INTERVAL_MS[interval]
        a = int(_aware(start).timestamp() * 1000)
        b = int(_aware(end).timestamp() * 1000)
        if b <= a:
            return 0
        return (b - a) // step
