"""Poll illustrative sources. An empty minute does not call the analyst."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.ai.analyst import MockAIAnalyst
from app.ai.policy import regime_from_analysis
from app.market.universe import START_UNIVERSE
from app.news.entities import link_entities
from app.news.events import EventStore
from app.news.sources.mock import RawItem
from app.news.status import ExchangeStatusSnapshot
from app.schemas.news import assert_payload_is_data
from app.schemas.risk import RiskRegime


@dataclass(frozen=True)
class PollResult:
    skipped: bool
    reason: str
    new_events: int
    duplicates: int
    dropped: int
    analyst_calls: int


class NewsIngestor:
    def __init__(
        self,
        sources: list[object],
        store: EventStore,
        analyst: MockAIAnalyst,
        *,
        interval_sec: int = 60,
        stale_after_sec: int = 600,
        pause_if_unverified_high_impact: bool = True,
        watchlist: list[str] | None = None,
    ) -> None:
        if interval_sec < 60:
            raise ValueError("news_poll_interval_sec")
        self.sources = sources
        self.store = store
        self.analyst = analyst
        self.interval_sec = interval_sec
        self.stale_after_sec = stale_after_sec
        self.pause_if_unverified_high_impact = pause_if_unverified_high_impact
        self.watchlist = list(watchlist) if watchlist is not None else sorted(START_UNIVERSE)
        self.last_success_at: datetime | None = None
        self.analyst_calls = 0
        self._last_attempt_at: datetime | None = None

    def regime_for(self, symbol: str, now: datetime) -> RiskRegime:
        return self.store.regime_for(symbol, now, self.stale_after_sec)

    def poll(
        self,
        now: datetime,
        exchange_status: ExchangeStatusSnapshot | None = None,
    ) -> PollResult:
        if now.tzinfo is None:
            raise ValueError("naive_timestamp")
        for source in self.sources:
            if not getattr(source, "illustrative", False):
                raise ValueError("illustrative_source_required")
        if self._last_attempt_at is not None:
            elapsed = (now - self._last_attempt_at).total_seconds()
            if elapsed < self.interval_sec:
                if exchange_status is not None:
                    self.store.apply_exchange_status(exchange_status)
                return PollResult(True, "interval", 0, 0, 0, 0)
        self._last_attempt_at = now
        if exchange_status is not None:
            self.store.apply_exchange_status(exchange_status)
        try:
            items: list[RawItem] = []
            for source in self.sources:
                items.extend(source.fetch(now))  # type: ignore[attr-defined]
        except Exception:
            return PollResult(False, "source_error", 0, 0, 0, 0)
        new_events = 0
        duplicates = 0
        dropped = 0
        calls = 0
        for item in items:
            if self.store.match(item) is not None:
                self.store.add_sighting(item, now)
                duplicates += 1
                continue
            symbols = link_entities(item.headline, item.summary, self.watchlist)
            if not symbols:
                dropped += 1
                continue
            calls += 1
            self.analyst_calls += 1
            regime = self._classify(item)
            self.store.add_event(item, symbols, regime, now)
            new_events += 1
        self.last_success_at = now
        return PollResult(False, "ok", new_events, duplicates, dropped, calls)

    def _classify(self, item: RawItem) -> RiskRegime:
        payload: object = item.analysis if item.analysis is not None else item.headline
        if isinstance(payload, dict):
            try:
                assert_payload_is_data(payload)
            except TypeError:
                return RiskRegime.PAUSE_BUYS
        try:
            interpreted = self.analyst.analyze(payload)
        except Exception:
            return RiskRegime.PAUSE_BUYS
        if not interpreted.ok or interpreted.analysis is None:
            return RiskRegime.PAUSE_BUYS
        return regime_from_analysis(
            interpreted.analysis,
            pause_if_unverified_high_impact=self.pause_if_unverified_high_impact,
        )
