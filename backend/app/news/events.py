"""Event history. first_seen_at does not move when a story is reprinted."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import NewsEventRow
from app.news.dedupe import canonical_event_id, normalize_url, same_story, text_fingerprint
from app.news.sources.mock import RawItem
from app.news.status import ExchangeStatusSnapshot
from app.schemas.risk import RiskRegime

_RANK: dict[RiskRegime, int] = {
    RiskRegime.NORMAL: 0,
    RiskRegime.CAUTION: 1,
    RiskRegime.HIGH_RISK: 2,
    RiskRegime.PAUSE_BUYS: 3,
    RiskRegime.DEPEG: 4,
    RiskRegime.EXCHANGE_ISSUE: 5,
    RiskRegime.HALT_ALL: 6,
}


def stricter(left: RiskRegime, right: RiskRegime) -> RiskRegime:
    if _RANK[left] >= _RANK[right]:
        return left
    return right


@dataclass
class StoredEvent:
    canonical_event_id: str
    fingerprint: str
    headline: str
    summary: str
    links: list[str]
    sources: list[str]
    symbols: list[str]
    published_at: datetime | None
    first_seen_at: datetime
    fetched_at: datetime
    seen_count: int
    regime: RiskRegime
    illustrative: bool = True

    def weight(self, now: datetime, stale_after_sec: int) -> Decimal:
        """Linear decay from 1 at first_seen_at to 0 at stale_after_sec. Whole seconds."""

        if stale_after_sec <= 0:
            return Decimal("0")
        if now.tzinfo is None or self.first_seen_at.tzinfo is None:
            raise ValueError("naive_timestamp")
        delta = now - self.first_seen_at
        age = delta.days * 86400 + delta.seconds
        if age < 0:
            return Decimal("1")
        if age >= stale_after_sec:
            return Decimal("0")
        window = Decimal(stale_after_sec)
        return (window - Decimal(age)) / window


@dataclass
class EventStore:
    events: list[StoredEvent] = field(default_factory=list)
    exchange_status: ExchangeStatusSnapshot | None = None
    _by_url: dict[str, StoredEvent] = field(default_factory=dict)
    _by_fp: dict[str, StoredEvent] = field(default_factory=dict)

    def match(self, item: RawItem) -> StoredEvent | None:
        url = normalize_url(item.url)
        found = self._by_url.get(url)
        if found is not None:
            return found
        fingerprint = text_fingerprint(item.headline, item.summary)
        found = self._by_fp.get(fingerprint)
        if found is not None:
            return found
        for event in self.events:
            if same_story(fingerprint, event.fingerprint):
                return event
        return None

    def add_event(self, item: RawItem, symbols: list[str], regime: RiskRegime, now: datetime) -> StoredEvent:
        if now.tzinfo is None:
            raise ValueError("naive_timestamp")
        fingerprint = text_fingerprint(item.headline, item.summary)
        event = StoredEvent(
            canonical_event_id=canonical_event_id(fingerprint),
            fingerprint=fingerprint,
            headline=item.headline,
            summary=item.summary,
            links=[item.url],
            sources=[item.source],
            symbols=list(symbols),
            published_at=item.published_at,
            first_seen_at=now,
            fetched_at=now,
            seen_count=1,
            regime=regime,
        )
        self.events.append(event)
        self._by_url[normalize_url(item.url)] = event
        if fingerprint:
            self._by_fp[fingerprint] = event
        return event

    def add_sighting(self, item: RawItem, now: datetime) -> StoredEvent:
        event = self.match(item)
        if event is None:
            raise KeyError("unknown event")
        if now.tzinfo is None:
            raise ValueError("naive_timestamp")
        url = normalize_url(item.url)
        known = {normalize_url(link) for link in event.links}
        if url not in known:
            event.links.append(item.url)
        self._by_url[url] = event
        if item.source not in event.sources:
            event.sources.append(item.source)
        event.seen_count += 1
        event.fetched_at = now
        if event.published_at is None and item.published_at is not None:
            event.published_at = item.published_at
        return event

    def apply_exchange_status(self, snapshot: ExchangeStatusSnapshot) -> None:
        self.exchange_status = snapshot

    def regime_for(self, symbol: str, now: datetime, stale_after_sec: int) -> RiskRegime:
        regime = RiskRegime.NORMAL
        token = symbol.strip().upper()
        for event in self.events:
            if event.weight(now, stale_after_sec) <= 0:
                continue
            if token not in event.symbols and "MARKET" not in event.symbols:
                continue
            regime = stricter(regime, event.regime)
        return stricter(regime, self._status_regime(token, now, stale_after_sec))

    def _status_regime(self, symbol: str, now: datetime, stale_after_sec: int) -> RiskRegime:
        snapshot = self.exchange_status
        if snapshot is None:
            return RiskRegime.NORMAL
        if now.tzinfo is None:
            raise ValueError("naive_timestamp")
        delta = now - snapshot.observed_at
        age = delta.days * 86400 + delta.seconds
        if age >= stale_after_sec:
            return RiskRegime.NORMAL
        if snapshot.affected_assets and symbol not in snapshot.affected_assets:
            return RiskRegime.NORMAL
        if snapshot.status == "ok":
            return RiskRegime.NORMAL
        if snapshot.status == "degraded":
            return RiskRegime.HIGH_RISK
        return RiskRegime.EXCHANGE_ISSUE


def save_news(session: Session, store: EventStore) -> None:
    session.execute(delete(NewsEventRow))
    for event in store.events:
        session.add(_row_from_event(event))
    if store.exchange_status is not None:
        session.add(_row_from_status(store.exchange_status))
    session.commit()


def load_news(session: Session) -> EventStore:
    store = EventStore()
    rows = list(session.scalars(select(NewsEventRow)).all())
    for row in rows:
        if row.source == "exchange_status":
            venue = row.canonical_event_id.removeprefix("exchange-status:")
            assets = tuple(json.loads(row.symbols_json))
            store.exchange_status = ExchangeStatusSnapshot(
                venue=venue,
                status=row.headline,
                affected_assets=assets,
                observed_at=_aware(row.fetched_at),
            )
            continue
        event = StoredEvent(
            canonical_event_id=row.canonical_event_id,
            fingerprint=text_fingerprint(row.headline, row.summary),
            headline=row.headline,
            summary=row.summary,
            links=list(json.loads(row.links_json)),
            sources=list(json.loads(row.sources_json)),
            symbols=list(json.loads(row.symbols_json)),
            published_at=_aware(row.published_at) if row.published_at is not None else None,
            first_seen_at=_aware(row.first_seen_at),
            fetched_at=_aware(row.fetched_at),
            seen_count=row.seen_count,
            regime=_regime(row.regime),
        )
        store.events.append(event)
        for link in event.links:
            store._by_url[normalize_url(link)] = event
        if event.fingerprint:
            store._by_fp[event.fingerprint] = event
    return store


def _row_from_event(event: StoredEvent) -> NewsEventRow:
    return NewsEventRow(
        canonical_event_id=event.canonical_event_id,
        source=event.sources[0] if event.sources else "mock",
        url=event.links[0] if event.links else "",
        headline=event.headline,
        summary=event.summary,
        published_at=event.published_at,
        first_seen_at=event.first_seen_at,
        fetched_at=event.fetched_at,
        links_json=json.dumps(event.links, separators=(",", ":")),
        symbols_json=json.dumps(event.symbols, separators=(",", ":")),
        sources_json=json.dumps(event.sources, separators=(",", ":")),
        regime=event.regime.value,
        seen_count=event.seen_count,
    )


def _row_from_status(snapshot: ExchangeStatusSnapshot) -> NewsEventRow:
    return NewsEventRow(
        canonical_event_id=f"exchange-status:{snapshot.venue}",
        source="exchange_status",
        url=f"status://{snapshot.venue}",
        headline=snapshot.status,
        summary="",
        published_at=None,
        first_seen_at=snapshot.observed_at,
        fetched_at=snapshot.observed_at,
        links_json="[]",
        symbols_json=json.dumps(list(snapshot.affected_assets), separators=(",", ":")),
        sources_json=json.dumps(["exchange_status"], separators=(",", ":")),
        regime="NORMAL",
        seen_count=1,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _regime(value: str) -> RiskRegime:
    try:
        return RiskRegime(value)
    except ValueError:
        return RiskRegime.PAUSE_BUYS
