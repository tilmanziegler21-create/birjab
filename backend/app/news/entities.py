"""Link a fixture to a watchlist asset. English words are not tickers."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ALIASES: tuple[tuple[str, str], ...] = (
    ("binance coin", "BNB"),
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("chainlink", "LINK"),
    ("avalanche", "AVAX"),
    ("cardano", "ADA"),
    ("solana", "SOL"),
    ("ripple", "XRP"),
)

_ORDER: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "ADA", "AVAX", "MARKET")

# Uppercase tokens that are also ordinary words. `$ONE` or `ticker ONE` only.
_AMBIGUOUS = frozenset({"ONE", "ALL", "FOR", "NOW", "AIR", "DATA"})


def link_entities(headline: str, summary: str, watchlist: Iterable[str]) -> list[str]:
    allowed = {item.strip().upper() for item in watchlist if item.strip()}
    text = f"{headline}\n{summary}"
    lowered = text.lower()
    found: list[str] = []
    for alias, symbol in _ALIASES:
        if symbol in allowed and re.search(rf"\b{re.escape(alias)}\b", lowered):
            found.append(symbol)
    for match in re.findall(r"\$([A-Za-z]{2,10})\b", text):
        symbol = match.upper()
        if symbol in allowed and _ticker_token_ok(text, symbol):
            found.append(symbol)
    for match in re.findall(r"\b([A-Z]{2,10})\b", text):
        if match in _AMBIGUOUS or match not in allowed:
            continue
        found.append(match)
    for match in re.findall(r"\bticker\s+([A-Za-z]{2,10})\b", text, flags=re.IGNORECASE):
        symbol = match.upper()
        if symbol in allowed:
            found.append(symbol)
    assets = [symbol for symbol in found if symbol != "MARKET"]
    market = False
    if re.search(r"\bsec\b", lowered) or "market-wide" in lowered or "marketwide" in lowered:
        market = True
    if re.search(r"\bdepeg\b", lowered):
        market = True
    if re.search(r"\b(hack|exploit)\b", lowered) and re.search(r"\bexchange\b", lowered):
        market = True
    broad = re.search(
        r"\b(hack|exploit|delist|delisting|halt|halted|outage|suspend|suspension)\b",
        lowered,
    )
    if broad and not assets:
        market = True
    if market:
        found.append("MARKET")
    ordered: list[str] = []
    for symbol in (*_ORDER, *sorted(allowed)):
        if symbol in found and symbol not in ordered:
            ordered.append(symbol)
    return ordered


def _ticker_token_ok(text: str, symbol: str) -> bool:
    if symbol not in _AMBIGUOUS:
        return True
    if re.search(rf"\${symbol}\b", text):
        return True
    return re.search(rf"\bticker\s+{symbol}\b", text, flags=re.IGNORECASE) is not None
