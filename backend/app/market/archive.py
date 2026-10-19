"""Download Binance Vision spot kline zips. GET only, no API key, no order path."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from app.market.history import CandleStore
from app.market.public import Candle
from app.money import money

ARCHIVE_HOST = "data.binance.vision"
PREFIX = "/data/spot/"


class ArchiveError(ValueError):
    pass


def _as_millis(value: int) -> int:
    """Binance Vision files use ms; some dumps are µs or ns."""

    if value >= 10**18:
        return value // 1_000_000
    if value >= 10**15:
        return value // 1000
    return value


def assert_archive_url(url: str) -> None:
    if not url.startswith(f"https://{ARCHIVE_HOST}{PREFIX}"):
        raise ArchiveError("archive_host_not_allowlisted")
    if ".." in url or "signature=" in url.lower() or "order" in url.lower():
        raise ArchiveError("archive_url_forbidden")


def archive_url(symbol: str, interval: str, day: date | None = None, month: str | None = None) -> str:
    pair = symbol.replace("/", "").upper()
    if month:
        return f"https://{ARCHIVE_HOST}{PREFIX}monthly/klines/{pair}/{interval}/{pair}-{interval}-{month}.zip"
    if day is None:
        raise ArchiveError("day_or_month_required")
    stamp = day.isoformat()
    return f"https://{ARCHIVE_HOST}{PREFIX}daily/klines/{pair}/{interval}/{pair}-{interval}-{stamp}.zip"


def fetch_zip(url: str, *, opener: Callable[..., object] | None = None, timeout: float = 60) -> bytes:
    from app.market.http_get import archive_get

    return archive_get(url, opener=opener, timeout=timeout)


def parse_kline_zip(body: bytes, *, interval: str) -> list[Candle]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        raise ArchiveError("bad_zip") from exc
    names = [name for name in archive.namelist() if name.endswith(".csv")]
    if not names:
        raise ArchiveError("zip_has_no_csv")
    candles: list[Candle] = []
    seen: set[int] = set()
    with archive.open(names[0]) as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8")
        reader = csv.reader(text)
        for row in reader:
            if not row or row[0] in {"open_time", "Open time"}:
                continue
            try:
                raw_ts = int(row[0])
            except (TypeError, ValueError):
                continue
            ms = _as_millis(raw_ts)
            try:
                opened = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
            if opened.year < 2017 or opened.year > 2100:
                continue
            if ms in seen:
                continue
            seen.add(ms)
            candles.append(
                Candle(
                    open_time=opened,
                    open_price=money(row[1]),
                    high=money(row[2]),
                    low=money(row[3]),
                    close=money(row[4]),
                    volume=money(row[5]),
                    interval=interval,
                )
            )
    return candles


def month_stamps(start: date, end: date) -> list[str]:
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    out: list[str] = []
    while cursor <= last:
        out.append(f"{cursor.year:04d}-{cursor.month:02d}")
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return out


def sync_range(
    store: CandleStore,
    symbol: str,
    interval: str,
    start: date,
    end: date,
    *,
    fetch: Callable[[str], bytes] = fetch_zip,
) -> dict[str, object]:
    """Pull monthly zips for the closed months, daily zips for the current partial month."""

    added = 0
    failed: list[str] = []
    fetched = 0
    today = datetime.now(timezone.utc).date()
    last_full_month = date(today.year, today.month, 1) - timedelta(days=1)
    last_full_month = date(last_full_month.year, last_full_month.month, 1)
    for month in month_stamps(start, min(end, last_full_month) if last_full_month >= start else start):
        year, mon = int(month[:4]), int(month[5:7])
        month_start = date(year, mon, 1)
        if month_start < start and month != f"{start.year:04d}-{start.month:02d}":
            continue
        url = archive_url(symbol, interval, month=month)
        try:
            body = fetch(url)
            candles = parse_kline_zip(body, interval=interval)
            added += store.upsert(symbol, interval, candles)
            fetched += 1
        except (ArchiveError, ValueError, OSError):
            failed.append(url)
    cursor = max(start, date(today.year, today.month, 1))
    while cursor <= end and cursor <= today:
        url = archive_url(symbol, interval, day=cursor)
        try:
            body = fetch(url)
            candles = parse_kline_zip(body, interval=interval)
            added += store.upsert(symbol, interval, candles)
            fetched += 1
        except (ArchiveError, ValueError, OSError):
            failed.append(url)
        cursor += timedelta(days=1)
    return {"symbol": symbol, "interval": interval, "added": added, "fetched": fetched, "failed": len(failed), "coverage": store.coverage(symbol, interval)}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
