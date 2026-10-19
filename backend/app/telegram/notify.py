"""Notifications: detect what changed between two /api/state snapshots, then throttle.

Only real runtime facts are used: ledger fills and lot status, RiskManager blocks, drawdown,
news regimes, feed status, runtime errors, manual controls. Nothing is inferred or invented.

`EventWatcher.seen` is the "last sent event id" set. It is written to a JSON file after every
observation so that a restarted Telegram process announces only what happened while it was
down and never replays the whole history. Without a state file the watcher primes on the
current snapshot and stays silent about the past.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any

from app.telegram import views
from app.telegram.client import StateClient, StateUnavailable

log = logging.getLogger("ats.telegram")

Send = Callable[[str], Awaitable[None]]
ARM_NAMES = views.ARMS


@dataclass(frozen=True)
class Event:
    key: str
    kind: str
    text: str
    critical: bool = False


@dataclass
class Seen:
    fills: dict[str, list[str]] = field(default_factory=dict)
    lot_status: dict[str, dict[str, str]] = field(default_factory=dict)
    blocked: dict[str, bool] = field(default_factory=dict)
    drawdown_alerted: dict[str, bool] = field(default_factory=dict)
    regimes: dict[str, str] = field(default_factory=dict)
    exchange_status: str | None = None
    news_ids: list[str] = field(default_factory=list)
    feed_ok: dict[str, bool] = field(default_factory=dict)
    feed_stale: dict[str, bool] = field(default_factory=dict)
    last_error_at: str | None = None
    last_control_at: str | None = None
    tick_count: int = 0
    started_at: str | None = None
    api_down: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Seen:
        known = {k: payload[k] for k in cls.__dataclass_fields__ if k in payload}
        return cls(**known)


class EventWatcher:
    def __init__(self, *, drawdown_alert_pct: Decimal = Decimal("5"), state_path: Path | None = None) -> None:
        self.drawdown_alert_pct = drawdown_alert_pct
        self.state_path = state_path
        self.seen = Seen()
        self.primed = False
        self.last_digest_at: datetime | None = None
        self.loaded_from_disk = False
        if state_path is not None:
            self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        assert self.state_path is not None
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            log.warning("telegram state file unreadable; starting from the current state")
            return
        seen = raw.get("seen")
        if not isinstance(seen, dict):
            return
        self.seen = Seen.from_json(seen)
        digest = raw.get("last_digest_at")
        self.last_digest_at = views.when(digest) if digest else None
        self.primed = True
        self.loaded_from_disk = True

    def save(self) -> None:
        if self.state_path is None:
            return
        payload: dict[str, Any] = {}
        if self.state_path.is_file():
            try:
                existing = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload = existing
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload.update(
            {
                "version": 1,
                "seen": self.seen.to_json(),
                "last_digest_at": self.last_digest_at.isoformat() if self.last_digest_at else None,
                "saved_at": views.utcnow().isoformat(),
            }
        )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), prefix=".telegram_state.", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError:
            log.warning("could not persist telegram state", exc_info=True)
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    # ------------------------------------------------------------------ observation

    def prime(self, state: dict[str, Any], now: datetime) -> None:
        self._observe(state, now)
        self.primed = True
        self.save()

    def observe(self, state: dict[str, Any], now: datetime) -> list[Event]:
        if not self.primed:
            self.prime(state, now)
            return []
        events = self._observe(state, now)
        self.save()
        return events

    def _observe(self, state: dict[str, Any], now: datetime) -> list[Event]:
        events: list[Event] = []
        runtime = state["runtime"]
        q = views.quote(state)

        # A restarted runtime (new started_at) is itself news when we were watching before.
        started = runtime.get("started_at")
        if self.primed and self.seen.started_at and started and started != self.seen.started_at:
            events.append(
                Event(
                    key=f"restart:{started}",
                    kind="runtime",
                    text=f"🔄 Торговый процесс перезапущен: восстановлено заявок {runtime.get('restored_orders', 0)}, тиков {runtime.get('tick_count')}.",
                )
            )
        self.seen.started_at = started

        for name in ARM_NAMES:
            arm = state["arms"][name]
            label = views.ARM_LABEL[name]
            seen_fills = self.seen.fills.setdefault(name, [])
            seen_set = set(seen_fills)
            for fill in arm.get("fills", []):
                fill_id = fill["fill_id"]
                if fill_id in seen_set:
                    continue
                seen_fills.append(fill_id)
                seen_set.add(fill_id)
                if not self.primed or name != "baseline":
                    continue
                if fill["side"] == "buy":
                    events.append(
                        Event(key=f"fill:{name}:{fill_id}", kind="fill", text=views.buy_notice(state, fill, name=name))
                    )
                else:
                    events.append(
                        Event(key=f"fill:{name}:{fill_id}", kind="fill", text=views.sell_notice(state, fill))
                    )
            statuses = self.seen.lot_status.setdefault(name, {})
            for lot in arm.get("lots", []):
                previous = statuses.get(lot["lot_id"])
                statuses[lot["lot_id"]] = lot["status"]
            blocked = bool(arm.get("trading_blocked"))
            if self.primed and blocked and not self.seen.blocked.get(name, False):
                events.append(
                    Event(
                        key=f"blocked:{name}:{arm.get('block_reason')}",
                        kind="drawdown",
                        text=f"⛔ <b>{label}</b>: торговля заблокирована — {escape(str(arm.get('block_reason')))}. Новых покупок не будет до ручной проверки.",
                        critical=True,
                    )
                )
            self.seen.blocked[name] = blocked
            dd = views.dec(arm.get("drawdown_pct"))
            over = dd is not None and dd >= self.drawdown_alert_pct
            if self.primed and over and not self.seen.drawdown_alerted.get(name, False):
                events.append(
                    Event(
                        key=f"drawdown:{name}",
                        kind="drawdown",
                        text=f"📉 <b>{label}</b>: просадка {views.money(dd)}% от пика {views.money(arm.get('peak_equity'))} {q} "
                        f"(порог уведомления {views.money(self.drawdown_alert_pct)}%).",
                        critical=True,
                    )
                )
            if dd is not None:
                self.seen.drawdown_alerted[name] = over

        # News: regime per asset, new abnormal events, exchange status.
        news = state["news"]
        for base, regime in news.get("regimes", {}).items():
            previous = self.seen.regimes.get(base)
            self.seen.regimes[base] = regime
            if self.primed and previous is not None and previous != regime:
                events.append(
                    Event(
                        key=f"regime:{base}:{regime}",
                        kind="regime",
                        text=f"📰 Режим риска {escape(base)}: {escape(previous)} → <b>{escape(regime)}</b>",
                        critical=regime in {"EXCHANGE_ISSUE", "HALT_ALL"},
                    )
                )
        news_seen = set(self.seen.news_ids)
        for event in news.get("events", []):
            cid = event["canonical_event_id"]
            if cid in news_seen:
                continue
            self.seen.news_ids.append(cid)
            news_seen.add(cid)
            if self.primed and event["regime"] != "NORMAL":
                kind = "TEST/MOCK" if event.get("illustrative") else "неподтверждено"
                links = event.get("links") or []
                link = escape(str(links[0])) if links else "—"
                events.append(
                    Event(
                        key=f"news:{cid}",
                        kind="news",
                        text=(
                            f"📰 <b>{escape(event['regime'])}</b> · {kind} · {escape(', '.join(event.get('symbols') or []))}\n"
                            f"источник: {escape(', '.join(event.get('sources') or []) or '—')} · ссылка: {link}\n"
                            f"{escape(str(event['headline'])[:200])}"
                        ),
                        critical=event["regime"] in {"EXCHANGE_ISSUE", "HALT_ALL"},
                    )
                )
        if len(self.seen.news_ids) > 2000:
            del self.seen.news_ids[:-1000]
        status = news.get("exchange_status")
        status_key = None if not status else f"{status.get('venue')}:{status.get('status')}"
        if self.primed and status and status_key != self.seen.exchange_status and status.get("status") != "ok":
            events.append(
                Event(
                    key=f"exchange:{status_key}",
                    kind="exchange",
                    text=f"🏦 Статус площадки {escape(str(status.get('venue')))}: <b>{escape(str(status.get('status')))}</b>",
                    critical=True,
                )
            )
        self.seen.exchange_status = status_key

        # Feed.
        lost: list[str] = []
        back: list[str] = []
        stale: list[str] = []
        feed = state["feed"]["symbols"]
        for symbol, st in feed.items():
            ok = bool(st.get("ok"))
            is_stale = bool(st.get("stale"))
            prev_ok = self.seen.feed_ok.get(symbol)
            prev_stale = self.seen.feed_stale.get(symbol, False)
            self.seen.feed_ok[symbol] = ok
            self.seen.feed_stale[symbol] = is_stale
            if not self.primed:
                continue
            if prev_ok is not False and not ok:
                lost.append(f"{symbol} ({escape(str(st.get('error')))})")
            elif prev_ok is False and ok:
                back.append(symbol)
            if is_stale and not prev_stale:
                stale.append(symbol)
        if lost:
            all_down = all(not s.get("ok") for s in feed.values())
            events.append(
                Event(
                    key="feed:lost:" + ("all" if all_down else ",".join(sorted(lost))),
                    kind="feed",
                    text=("📡 Рыночная лента пропала полностью" if all_down else "📡 Лента недоступна: " + ", ".join(lost))
                    + "\nНовых заявок по этим парам нет.",
                    critical=all_down,
                )
            )
        if back:
            events.append(Event(key="feed:back:" + ",".join(sorted(back)), kind="feed", text="📡 Лента восстановлена: " + ", ".join(back)))
        if stale:
            events.append(Event(key="feed:stale:" + ",".join(sorted(stale)), kind="feed", text="📡 Книга не обновляется (stale): " + ", ".join(stale)))

        # Runtime errors and manual controls (from any interface). Both lists are tails, so compare by time.
        errors = runtime.get("errors", [])
        fresh_errors = [e for e in errors if self.seen.last_error_at is None or str(e["at"]) > self.seen.last_error_at]
        if self.primed and fresh_errors:
            events.append(
                Event(
                    key="errors:" + str(fresh_errors[-1]["error"])[:60],
                    kind="error",
                    text=f"⚠️ Сбой рантайма ({len(fresh_errors)}): " + escape(str(fresh_errors[-1]["error"])[:200]) + "\nЦикл продолжает работать.",
                    critical=True,
                )
            )
        if errors:
            self.seen.last_error_at = max(str(e["at"]) for e in errors)
        history = state["controls"].get("history", [])
        fresh_controls = [h for h in history if self.seen.last_control_at is None or str(h["at"]) > self.seen.last_control_at]
        if self.primed:
            for item in fresh_controls:
                events.append(Event(key=f"control:{item['action']}:{item['at']}", kind="control", text=f"🎛 Управление: <b>{escape(str(item['action']))}</b>"))
        if history:
            self.seen.last_control_at = max(str(h["at"]) for h in history)
        self.seen.tick_count = int(runtime.get("tick_count") or 0)
        return events

    def api_lost(self, reason: str) -> Event | None:
        if self.seen.api_down:
            return None
        self.seen.api_down = True
        self.save()
        return Event(
            key="api:down",
            kind="api",
            text=f"🔌 Локальный API торгового процесса недоступен: {escape(reason[:160])}. Уведомления возобновятся после восстановления.",
            critical=True,
        )

    def api_back(self) -> Event | None:
        if not self.seen.api_down:
            return None
        self.seen.api_down = False
        self.save()
        return Event(key="api:back", kind="api", text="🔌 Связь с торговым процессом восстановлена.")


class Throttle:
    def __init__(self, *, max_per_minute: int = 10, dedupe_sec: int = 600) -> None:
        self.max_per_minute = max_per_minute
        self.dedupe_sec = dedupe_sec
        self._last_by_key: dict[str, datetime] = {}
        self._window: deque[datetime] = deque()
        self.suppressed: dict[str, int] = {}

    def admit(self, event: Event, now: datetime) -> bool:
        last = self._last_by_key.get(event.key)
        if last is not None and now - last < timedelta(seconds=self.dedupe_sec):
            self._suppress(event.kind)
            return False
        while self._window and now - self._window[0] > timedelta(minutes=1):
            self._window.popleft()
        if len(self._window) >= self.max_per_minute and not event.critical:
            self._suppress(event.kind)
            return False
        self._window.append(now)
        self._last_by_key[event.key] = now
        if len(self._last_by_key) > 5000:
            for key in sorted(self._last_by_key, key=self._last_by_key.get)[:2500]:  # type: ignore[arg-type]
                del self._last_by_key[key]
        return True

    def take_suppressed(self) -> dict[str, int]:
        out, self.suppressed = self.suppressed, {}
        return out

    def _suppress(self, kind: str) -> None:
        self.suppressed[kind] = self.suppressed.get(kind, 0) + 1


class Notifier:
    """Polls the state client, emits events through an injected async `send`."""

    def __init__(
        self,
        client: StateClient,
        send: Send,
        *,
        max_per_minute: int = 10,
        digest_interval_sec: int = 3600,
        drawdown_alert_pct: Decimal = Decimal("5"),
        state_path: Path | None = None,
        clock: Callable[[], datetime] = views.utcnow,
    ) -> None:
        self.client = client
        self.send = send
        self.clock = clock
        self.watcher = EventWatcher(drawdown_alert_pct=drawdown_alert_pct, state_path=state_path)
        self.throttle = Throttle(max_per_minute=max_per_minute)
        self.digest_interval = timedelta(seconds=digest_interval_sec)
        self.retry: deque[str] = deque(maxlen=50)
        self.retry_backoff_sec = 30
        self._retry_after: datetime | None = None
        self.sent: int = 0
        self.failed: int = 0
        self._last_tick_seen = -1
        self.last_state: dict[str, Any] | None = None

    @property
    def last_digest_at(self) -> datetime | None:
        return self.watcher.last_digest_at

    def prime(self) -> dict[str, Any] | None:
        """First look. With a saved state file this replays only what we missed."""

        now = self.clock()
        try:
            state = self.client.state()
        except StateUnavailable as exc:
            log.warning("telegram: api unavailable at start (%s)", exc)
            return None
        self.last_state = state
        if not self.watcher.primed:
            self.watcher.prime(state, now)
        if self.watcher.last_digest_at is None:
            self.watcher.last_digest_at = now
            self.watcher.save()
        # Fresh start: the current tick is already accounted for by prime(). Restored from disk: let the
        # next poll diff the saved state against the live one and announce what happened meanwhile.
        self._last_tick_seen = -1 if self.watcher.loaded_from_disk else int(state["runtime"].get("tick_count") or 0)
        return state

    async def poll(self) -> list[str]:
        """One pass: retry failed sends, emit new events if a tick happened, maybe a digest."""

        now = self.clock()
        delivered: list[str] = []
        await self._flush_retry(delivered)
        try:
            state = self.client.state()
        except StateUnavailable as exc:
            event = self.watcher.api_lost(str(exc))
            if event is not None and self.throttle.admit(event, now):
                await self._deliver(event.text, delivered)
            return delivered
        back = self.watcher.api_back()
        if back is not None and self.throttle.admit(back, now):
            await self._deliver(back.text, delivered)
        self.last_state = state
        tick = int(state["runtime"].get("tick_count") or 0)
        if not self.watcher.primed:
            self.watcher.prime(state, now)
            self._last_tick_seen = tick
        elif tick != self._last_tick_seen:
            self._last_tick_seen = tick
            try:
                events = self.watcher.observe(state, now)
            except Exception:  # noqa: BLE001 - never let a detector bug stop polling
                log.exception("telegram watcher failed")
                events = []
            for event in events:
                if self.throttle.admit(event, now):
                    await self._deliver(event.text, delivered)
        if self.watcher.last_digest_at is None:
            self.watcher.last_digest_at = now
            self.watcher.save()
        elif now - self.watcher.last_digest_at >= self.digest_interval:
            self.watcher.last_digest_at = now
            self.watcher.save()
            try:
                text = views.digest_screen(state, now, self.throttle.take_suppressed())
            except Exception:  # noqa: BLE001
                log.exception("telegram digest failed")
                text = "Сводка недоступна: ошибка при подготовке. Торговый процесс не затронут."
            await self._deliver(text, delivered)
        return delivered

    async def announce(self, text: str) -> None:
        await self._deliver(text, [])

    async def _flush_retry(self, delivered: list[str]) -> None:
        if not self.retry or (self._retry_after is not None and self.clock() < self._retry_after):
            return
        pending = list(self.retry)
        self.retry.clear()
        for index, text in enumerate(pending):
            await self._deliver(text, delivered)
            if self._retry_after is not None and self.clock() < self._retry_after:
                # The first failure re-arms the backoff; keep the rest queued without hammering.
                self.retry.extend(pending[index + 1 :])
                break

    async def _deliver(self, text: str, delivered: list[str]) -> None:
        try:
            await self.send(text)
        except Exception as exc:  # noqa: BLE001 - network errors must not reach the runtime
            self.failed += 1
            self.retry.append(text)
            self._retry_after = self.clock() + timedelta(seconds=self.retry_backoff_sec)
            log.warning("telegram send failed (%s); queued for retry (%d waiting)", type(exc).__name__, len(self.retry))
            return
        self._retry_after = None
        self.sent += 1
        delivered.append(text)
