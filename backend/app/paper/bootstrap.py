"""Wire a runtime from settings. The only outbound call possible is a public GET."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import Engine

from app.ai.analyst import MockAIAnalyst
from app.config import Settings
from app.db.session import arm_database_url, init_db, make_engine
from app.market.http_get import read_only_get
from app.market.public import PublicBookClient
from app.news.events import EventStore
from app.news.ingest import NewsIngestor
from app.news.sources.mock import MockNewsSource, RawItem
from app.paper.feed import PublicFeed, SyntheticFeed
from app.paper.runtime import ARM_NAMES, PaperRuntime


def build_feed(settings: Settings) -> SyntheticFeed | PublicFeed:
    symbols = [f"{base}/{settings.quote_currency.value}" for base in settings.watchlist]
    if settings.exchange == "disabled":
        raise ValueError("exchange is disabled in this config; no feed and no runtime")
    if settings.exchange == "binance_public":
        return PublicFeed(PublicBookClient(read_only_get))
    return SyntheticFeed(symbols, seed=settings.synthetic_seed)


def load_fixtures(path: str | Path) -> list[RawItem]:
    """Illustrative headlines from a local JSON file. They are not news."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("illustrative") is not True:
        raise ValueError("news fixtures must be an object with \"illustrative\": true")
    items: list[RawItem] = []
    for row in raw.get("items", []):
        if not isinstance(row, dict):
            raise ValueError("fixture item must be an object")
        published = row.get("published_at")
        published_at = None
        if isinstance(published, str):
            published_at = datetime.fromisoformat(published)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        analysis = row.get("analysis")
        items.append(
            RawItem(
                source=str(row.get("source", "fixture")),
                url=str(row["url"]),
                headline=str(row["headline"]),
                summary=str(row.get("summary", "")),
                published_at=published_at,
                analysis=analysis if isinstance(analysis, dict) else None,
            )
        )
    return items


def build_news(settings: Settings) -> NewsIngestor:
    items = load_fixtures(settings.news_fixtures_path) if settings.news_fixtures_path else []
    return NewsIngestor(
        [MockNewsSource(items)],
        EventStore(),
        MockAIAnalyst(),
        interval_sec=settings.news_poll_interval_sec,
        stale_after_sec=settings.news_stale_after_sec,
        pause_if_unverified_high_impact=settings.pause_new_buys_if_unverified_high_impact,
        watchlist=list(settings.watchlist),
    )


def build_engines(settings: Settings) -> dict[str, Engine]:
    url = settings.database_url
    engines: dict[str, Engine] = {}
    if url.startswith("sqlite"):
        engines["shared"] = make_engine(url)
        for arm in ARM_NAMES:
            engines[arm] = make_engine(arm_database_url(url, arm))
    else:
        engines["shared"] = make_engine(url)
        for arm in ARM_NAMES:
            engines[arm] = make_engine(url, schema=arm)
    for engine in engines.values():
        init_db(engine)
    return engines


def build_runtime(settings: Settings, *, persist: bool = True) -> PaperRuntime:
    engines = build_engines(settings) if persist else {}
    return PaperRuntime(settings, build_feed(settings), build_news(settings), engines=engines)
