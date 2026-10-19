"""Telegram surface: allowlist, read-only commands, /whoami, notifications, dedupe, restart recovery,
remote (separate-process) client over the local API, single polling instance, no path to orders."""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db.session import init_db, make_engine
from app.main import create_app
from app.news.sources.mock import RawItem
from app.paper.feed import ScriptedFeed
from app.paper.runtime import PaperRuntime
from app.schemas.orders import OrderState
from app.telegram import views
from app.telegram.auth import Allowlist
from app.telegram.client import LocalClient, RemoteClient, StateUnavailable
from app.telegram.commands import CommandService, Reply
from app.telegram.notify import Event, EventWatcher, Notifier, Throttle
from tests.conftest import NOW
from tests.test_stress import INCIDENT, _book, _fill_one_lot, _identity, _runtime

OWNER = 1001
STRANGER = 2002
TELEGRAM = Path(__file__).resolve().parents[1] / "app" / "telegram"


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: int) -> datetime:
        self.at = self.at + timedelta(seconds=seconds)
        return self.at


def _service(runtime: PaperRuntime, clock: Clock, *allowed: int, controls: bool = False) -> CommandService:
    return CommandService(LocalClient(runtime), Allowlist(frozenset(allowed or (OWNER,))), controls_enabled=controls, clock=clock)


def _confirm_button(reply: Reply) -> str:
    return next(data for row in reply.buttons for label, data in row if data.startswith("confirm:"))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------- authorisation


def test_allowlist_denies_unknown_users_quietly_and_never_leaks_secrets() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed, llm_api_key="local-test-key", exchange_api_key="local-test-key", telegram_bot_token="123:local-token")
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    clock = Clock(NOW)
    service = _service(runtime, clock)
    assert service.handle_text(STRANGER, "/start") == Reply("Доступ запрещён.")
    assert service.handle_text(STRANGER, "/start") is None  # one refusal per cooldown
    assert service.handle_text(STRANGER, "/halt") is None
    assert service.handle_callback(STRANGER, "cmd:halt") is None
    assert service.handle_text(None, "/balance") is None
    clock.advance(3601)
    assert service.handle_text(STRANGER, "/balance") == Reply("Доступ запрещён.")
    assert runtime.controls.halt_all is False
    for command in ("/start", "/status", "/balance", "/positions", "/trades", "/news", "/report", "/pause", "/resume", "/halt"):
        reply = service.handle_text(OWNER, command)
        assert reply is not None
        assert "local-test-key" not in reply.text and "local-token" not in reply.text
        assert "api_key" not in reply.text.lower()
    assert "PAPER" in service.handle_text(OWNER, "/start").text  # type: ignore[union-attr]
    assert "/liquidate" not in service.handle_text(OWNER, "/foo").text  # type: ignore[union-attr]


def test_whoami_works_for_anyone_without_trading_data_and_groups_are_ignored() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    service = _service(runtime, Clock(NOW))
    reply = service.handle_text(STRANGER, "/whoami")
    assert reply == Reply(f"Ваш Telegram user id: <code>{STRANGER}</code>")
    assert "equity" not in reply.text.lower() and reply.buttons == []
    assert service.handle_text(OWNER, "/whoami@some_bot").text.endswith(f"<code>{OWNER}</code>")  # type: ignore[union-attr]
    # Group chats: even the owner gets nothing except /whoami.
    assert service.handle_text(OWNER, "/balance", private=False) is None
    assert service.handle_callback(OWNER, "cmd:status", private=False) is None
    assert service.handle_text(OWNER, "/whoami", private=False) is not None


def test_first_version_is_read_only_by_default() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    _fill_one_lot(runtime, feed, NOW)
    service = _service(runtime, Clock(NOW))
    for command in ("/pause", "/halt", "/resume"):
        reply = service.handle_text(OWNER, command)
        assert reply is not None and "только читает" in reply.text
    assert service.handle_callback(OWNER, "cmd:halt").text.startswith("Эта версия")  # type: ignore[union-attr]
    assert runtime.controls.pause_new_buys is False and runtime.controls.halt_all is False
    assert not any("HALT" in label for row in service.menu for label, _ in row)
    assert service.audit == []


def test_telegram_sources_cannot_reach_orders_or_the_live_adapter() -> None:
    banned_names = {"place_order", "apply_buy_fill", "apply_sell_fill", "reserve", "release", "LiveAdapter", "request_liquidate", "CONFIRM_LIQUIDATE", "match_resting", "liquidate"}
    banned_modules = {"app.adapters", "app.accounting.ledger", "app.execution", "app.risk.emergency", "app.paper", "app.db", "sqlalchemy"}
    for path in TELEGRAM.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    assert not any(name == prefix or name.startswith(prefix + ".") for prefix in banned_modules), f"{path.name} imports {name}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned_names, f"{path.name} touches {node.attr}"
            if isinstance(node, ast.Name):
                assert node.id not in banned_names, f"{path.name} names {node.id}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "/api/orders" not in node.value and "liquidate" not in node.value, f"{path.name} mentions an order route"


def test_remote_client_only_knows_the_local_api_and_three_controls() -> None:
    with pytest.raises(ValueError):
        RemoteClient("http://example.com:8000")
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, body: bytes | None) -> bytes:
        calls.append((method, url))
        if url.endswith("/api/state"):
            return json.dumps({"arms": {}}).encode()
        if url.endswith("/api/controls/pause"):
            return b'{"controls": {"pause_new_buys": true}}'
        raise AssertionError(url)

    client = RemoteClient("http://127.0.0.1:8000", transport=transport)
    assert client.state() == {"arms": {}}
    assert client.control("pause")["controls"]["pause_new_buys"] is True
    with pytest.raises(ValueError):
        client.control("liquidate")
    with pytest.raises(ValueError):
        client.control("orders")
    assert calls == [("GET", "http://127.0.0.1:8000/api/state"), ("POST", "http://127.0.0.1:8000/api/controls/pause")]

    def dead(method: str, url: str, body: bytes | None) -> bytes:
        raise StateUnavailable("connection refused")

    service = CommandService(RemoteClient("http://127.0.0.1:8000", transport=dead), Allowlist(frozenset({OWNER})), clock=Clock(NOW))
    reply = service.handle_text(OWNER, "/status")
    assert reply is not None and "недоступен" in reply.text and "connection refused" in reply.text


def test_remote_client_against_the_fastapi_app_renders_every_screen() -> None:
    from fastapi.testclient import TestClient

    feed = ScriptedFeed()
    runtime, _ = _runtime(feed, items=[RawItem(source="illustrative", url="https://example.org/i", headline="Bitcoin network incident rumour", analysis=INCIDENT)])
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    at = NOW + timedelta(seconds=65)
    feed.set_book(_book("99.98", "100.00", at))
    runtime.tick(at)
    at = _fill_one_lot(runtime, feed, at + timedelta(seconds=5))
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    runtime.tick(at)
    app = create_app(runtime.settings, runtime=runtime)
    with TestClient(app) as http:

        def transport(method: str, url: str, body: bytes | None) -> bytes:
            path = url.split("8000", 1)[1]
            response = http.request(method, path, content=body)
            return response.content

        client = RemoteClient("http://127.0.0.1:8000", transport=transport)
        state = client.state()
        assert state["live_trading"] is False and json.dumps(state)  # plain JSON, no Decimals
        service = CommandService(client, Allowlist(frozenset({OWNER})), clock=Clock(at))
        news = service.handle_text(OWNER, "/news").text  # type: ignore[union-attr]
        assert "MOCK" in news and "Реальные новости и LLM пока не подключены" in news
        assert "Bitcoin network incident" in news and "&lt;" not in news
        service.handle_text(OWNER, "/expert")
        news_x = service.handle_text(OWNER, "/news").text  # type: ignore[union-attr]
        assert "TEST/MOCK" in news_x and "не LLM" in news_x and "https://example.org/i" in news_x
        assert "Последние вето фильтра" in news_x and "news_regime_pause_buys" in news_x
        service.handle_text(OWNER, "/simple")
        trades = service.handle_text(OWNER, "/trades").text  # type: ignore[union-attr]
        assert "Всего закрытых сделок: 1" in trades and "Купил:" in trades and "Прибыль:" in trades
        report = service.handle_text(OWNER, "/report").text  # type: ignore[union-attr]
        assert "A/B" in report and "независимых событий 1" in report
        start = service.handle_text(OWNER, "/start").text  # type: ignore[union-attr]
        assert "Результат" in start and "Общий капитал" in start and "A/B" not in start and "equity" not in start.lower()
        positions = service.handle_text(OWNER, "/positions").text  # type: ignore[union-attr]
        assert "Ждёт входа" in positions and "%" in positions
        balance = service.handle_text(OWNER, "/balance").text  # type: ignore[union-attr]
        assert "за 24 ч" in balance
        status = service.handle_text(OWNER, "/status").text  # type: ignore[union-attr]
        assert "Лента" in status and "восстановлено заявок" not in status
        service.handle_text(OWNER, "/expert")
        status_x = service.handle_text(OWNER, "/status").text  # type: ignore[union-attr]
        assert "восстановлено заявок" in status_x


# ---------------------------------------------------------------- controls (opt-in only)


def test_halt_needs_confirmation_and_a_second_press_is_a_noop_when_controls_are_enabled() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    clock = Clock(at)
    service = _service(runtime, clock, OWNER, 3003, controls=True)
    ask = service.handle_text(OWNER, "/halt")
    assert ask is not None and runtime.controls.halt_all is False
    token = _confirm_button(ask)
    assert "только тот" in service.handle_callback(3003, token).text  # type: ignore[union-attr]
    assert runtime.controls.halt_all is False
    done = service.handle_callback(OWNER, token)
    assert done is not None and "HALT включён" in done.text
    assert runtime.controls.halt_all is True and runtime.controls.liquidate_confirmed is False
    again = service.handle_callback(OWNER, token)
    assert again is not None and "уже использовано" in again.text
    assert service.audit[-1]["action"] == "halt_all" and len(service.audit) == 1
    at = clock.advance(5)
    feed.set_book(_book("98.90", "98.95", at))
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert len(arm.ledger.lots) == 1 and arm.ledger.reserved == Decimal("0")
        assert not any(o.intent.side == "buy" and o.state == OrderState.OPEN for o in arm.adapter.orders.values())
        assert any(o.intent.side == "sell" and o.state == OrderState.OPEN for o in arm.adapter.orders.values())
        assert not any(o.intent.purpose == "emergency_exit" for o in arm.adapter.orders.values())
    _identity(runtime)
    assert "HALT уже активен" in service.handle_text(OWNER, "/halt").text  # type: ignore[union-attr]
    ask = service.handle_text(OWNER, "/resume")
    assert ask is not None and "RiskManager" in ask.text
    ok = service.handle_callback(OWNER, _confirm_button(ask))
    assert ok is not None and runtime.controls.halt_all is False and runtime.controls.pause_new_buys is False
    at = clock.advance(5)
    feed.set_book(_book("98.90", "98.95", at))
    runtime.tick(at)
    for arm in runtime.arms.values():
        assert arm.ledger.reserved == Decimal("50")
    clock.advance(61)
    ask = service.handle_text(OWNER, "/halt")
    token = _confirm_button(ask)  # type: ignore[arg-type]
    clock.advance(61)
    assert "устарело" in service.handle_callback(OWNER, token).text  # type: ignore[union-attr]
    assert runtime.controls.halt_all is False


def test_pause_is_immediate_and_resume_does_not_bypass_risk_manager_when_enabled() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    clock = Clock(at)
    service = _service(runtime, clock, controls=True)
    assert "запрещены" in service.handle_text(OWNER, "/pause").text  # type: ignore[union-attr]
    assert "уже включена" in service.handle_callback(OWNER, "cmd:pause").text  # type: ignore[union-attr]
    assert runtime.controls.pause_new_buys is True
    runtime.halt(at)  # drop the resting entry so that resume has to propose a fresh one
    at = clock.advance(5)
    feed.set_book(_book("98.90", "98.95", at))
    runtime.tick(at)
    at = clock.advance(20)  # the book is now stale
    ask = service.handle_text(OWNER, "/resume")
    ok = service.handle_callback(OWNER, _confirm_button(ask))  # type: ignore[arg-type]
    assert ok is not None and runtime.controls.pause_new_buys is False and runtime.controls.halt_all is False
    runtime.tick(at)
    rejected = [d for d in runtime.decision_log if d["reason"] == "stale_market_feed"]
    assert rejected and all(not d["allowed"] for d in rejected)
    for arm in runtime.arms.values():
        assert all(lot.status != "closed" for lot in arm.ledger.lots.values())
        assert not any(o.intent.purpose == "emergency_exit" for o in arm.adapter.orders.values())
    runtime.arms["baseline"].ledger.mark_blocked("drawdown_limit")
    assert "заблокирована" in service.handle_text(OWNER, "/balance").text  # type: ignore[union-attr]
    assert runtime.arms["baseline"].ledger.trading_blocked is True


def test_html_in_headlines_is_escaped() -> None:
    feed = ScriptedFeed()
    item = RawItem(source="illustrative", url="https://example.org/x", headline="Bitcoin <b>BTC</b> <script>alert(1)</script> hack", analysis=INCIDENT)
    runtime, _ = _runtime(feed, items=[item])
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    text = views.news_screen(runtime.snapshot(), NOW)
    assert "<script>" not in text and "&lt;script&gt;" in text


# ---------------------------------------------------------------- notifications


def _watch(runtime: PaperRuntime, at: datetime) -> EventWatcher:
    watcher = EventWatcher()
    watcher.prime(runtime.snapshot(at), at)
    return watcher


def test_watcher_reports_fills_cycles_controls_and_nothing_twice() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    watcher = _watch(runtime, NOW)
    assert watcher.observe(runtime.snapshot(NOW), NOW) == []
    at = NOW + timedelta(seconds=5)
    feed.set_book(_book("98.90", "98.95", at))
    runtime.tick(at)
    events = watcher.observe(runtime.snapshot(at), at)
    assert [e.kind for e in events] == ["fill"]
    assert "Куплено BTC" in events[0].text and "позиция открыта" in events[0].text
    assert "Сетка + AI" not in events[0].text
    assert watcher.observe(runtime.snapshot(at), at) == []
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    runtime.tick(at)
    events = watcher.observe(runtime.snapshot(at), at)
    assert [e.kind for e in events] == ["fill"]
    assert "Продано BTC" in events[0].text and "Чистая прибыль" in events[0].text
    runtime.pause(at)
    events = watcher.observe(runtime.snapshot(at), at)
    assert [e.kind for e in events] == ["control"] and "pause_new_buys" in events[0].text


def test_watcher_reports_news_regime_feed_errors_drawdown_and_block() -> None:
    feed = ScriptedFeed()
    runtime, source = _runtime(feed)
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    watcher = _watch(runtime, NOW)
    source.items = [RawItem(source="illustrative", url="https://example.org/i", headline="Bitcoin network incident rumour", analysis=INCIDENT)]
    at = NOW + timedelta(seconds=65)
    feed.set_book(_book("99.98", "100.00", at))
    runtime.tick(at)
    events = watcher.observe(runtime.snapshot(at), at)
    assert [e.kind for e in events] == ["regime", "news"]
    assert "NORMAL → <b>PAUSE_BUYS</b>" in events[0].text and "TEST/MOCK" in events[1].text
    assert "https://example.org/i" in events[1].text
    assert watcher.observe(runtime.snapshot(at), at) == []
    at = at + timedelta(seconds=700)
    feed.set_book(_book("99.98", "100.00", at))
    runtime.tick(at)
    events = watcher.observe(runtime.snapshot(at), at)
    assert [e.kind for e in events] == ["regime"] and "PAUSE_BUYS → <b>NORMAL</b>" in events[0].text
    feed.outage = True
    at = at + timedelta(seconds=5)
    runtime.tick(at)
    events = watcher.observe(runtime.snapshot(at), at)
    assert len(events) == 1 and events[0].kind == "feed" and events[0].critical and "полностью" in events[0].text
    assert watcher.observe(runtime.snapshot(at), at) == []
    feed.outage = False
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.98", "100.00", at))
    runtime.tick(at)
    events = watcher.observe(runtime.snapshot(at), at)
    assert [e.kind for e in events] == ["feed"] and "восстановлена" in events[0].text
    runtime._error(at, "synthetic failure for the test")
    runtime.arms["ai"].ledger.mark_blocked("drawdown_limit")
    events = watcher.observe(runtime.snapshot(at), at)
    assert {e.kind for e in events} == {"error", "drawdown"}
    assert all(e.critical for e in events)
    assert "drawdown_limit" in next(e.text for e in events if e.kind == "drawdown")
    # Drawdown threshold: a one-time warning when drawdown_pct crosses the alert level.
    snap = runtime.snapshot(at)
    snap["arms"]["baseline"]["drawdown_pct"] = "6.5"
    events = watcher.observe(snap, at)
    assert [e.kind for e in events] == ["drawdown"] and "просадка 6.50%" in events[0].text
    assert watcher.observe(snap, at) == []
    snap["arms"]["baseline"]["drawdown_pct"] = "1.0"
    assert watcher.observe(snap, at) == []
    snap["arms"]["baseline"]["drawdown_pct"] = "7.0"
    assert [e.kind for e in watcher.observe(snap, at)] == ["drawdown"]  # re-armed after recovery


def test_throttle_folds_repeats_caps_per_minute_and_reports_what_it_dropped() -> None:
    throttle = Throttle(max_per_minute=3, dedupe_sec=600)
    at = NOW
    assert throttle.admit(Event("a", "fill", "x"), at)
    assert not throttle.admit(Event("a", "fill", "x"), at + timedelta(seconds=1))
    assert throttle.admit(Event("b", "fill", "x"), at)
    assert throttle.admit(Event("c", "fill", "x"), at)
    assert not throttle.admit(Event("d", "fill", "x"), at)  # cap
    assert throttle.admit(Event("e", "feed", "x", critical=True), at)  # critical bypasses the cap
    assert not throttle.admit(Event("e", "feed", "x", critical=True), at)  # but not the dedupe
    assert throttle.take_suppressed() == {"fill": 2, "feed": 1}
    assert throttle.take_suppressed() == {}
    assert throttle.admit(Event("d", "fill", "x"), at + timedelta(seconds=61))
    assert throttle.admit(Event("a", "fill", "x"), at + timedelta(seconds=601))


def test_notifier_survives_network_errors_retries_and_sends_digests() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    clock = Clock(NOW)
    sent: list[str] = []
    broken = {"on": False}

    async def send(text: str) -> None:
        if broken["on"]:
            raise ConnectionError("telegram unreachable")
        sent.append(text)

    notifier = Notifier(LocalClient(runtime, clock=clock), send, max_per_minute=10, digest_interval_sec=3600, clock=clock)
    notifier.prime()
    assert _run(notifier.poll()) == []  # primed on the existing state: no replay
    at = clock.advance(5)
    feed.set_book(_book("98.90", "98.95", at))
    runtime.tick(at)
    broken["on"] = True
    assert _run(notifier.poll()) == []
    assert notifier.failed == 1 and len(notifier.retry) == 1 and sent == []
    assert runtime.errors == [] and runtime.tick_count == 2  # the runtime never sees the failure
    clock.advance(5)
    assert _run(notifier.poll()) == [] and notifier.failed == 1  # backoff: no hammering
    broken["on"] = False
    clock.advance(30)
    delivered = _run(notifier.poll())
    assert len(delivered) == 1 and "Куплено BTC" in delivered[0] and len(notifier.retry) == 0
    clock.advance(3600)
    delivered = _run(notifier.poll())
    assert len(delivered) == 1 and delivered[0].startswith("<b>Сводка</b>")
    assert "Equity" in delivered[0] and "До входа" in delivered[0]


def test_notifier_reports_api_outage_once_and_recovery() -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    down = {"on": False}

    class Flaky(LocalClient):
        def state(self) -> dict[str, Any]:
            if down["on"]:
                raise StateUnavailable("connection refused")
            return super().state()

    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    clock = Clock(NOW)
    notifier = Notifier(Flaky(runtime), send, clock=clock)
    notifier.prime()
    down["on"] = True
    assert len(_run(notifier.poll())) == 1 and "недоступен" in sent[0]
    clock.advance(5)
    assert _run(notifier.poll()) == []  # once
    down["on"] = False
    clock.advance(5)
    delivered = _run(notifier.poll())
    assert len(delivered) == 1 and "восстановлена" in delivered[0]


def test_notifier_persists_seen_state_and_announces_only_what_it_missed(tmp_path: Path) -> None:
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    at = _fill_one_lot(runtime, feed, NOW)
    state_path = tmp_path / "telegram_state.json"
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    # 1) Fresh start without a state file: silent about history, writes the file.
    first = Notifier(LocalClient(runtime), send, state_path=state_path, clock=Clock(at))
    first.prime()
    assert _run(first.poll()) == [] and state_path.exists()
    saved = json.loads(state_path.read_text())
    assert len(saved["seen"]["fills"]["baseline"]) == 1 and saved["last_digest_at"] is not None

    # 2) While the bot is down, the cycle closes and a new entry rests.
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    runtime.tick(at)

    # 3) Restarted bot loads the file and reports exactly the missed sell + cycle, once.
    second = Notifier(LocalClient(runtime), send, state_path=state_path, clock=Clock(at))
    second.prime()
    assert second.watcher.loaded_from_disk is True
    delivered = _run(second.poll())
    assert len(delivered) == 1 and "Продано BTC" in delivered[0] and "Чистая прибыль" in delivered[0]
    assert _run(second.poll()) == []
    reloaded = json.loads(state_path.read_text())
    assert len(reloaded["seen"]["fills"]["baseline"]) == 2

    # 4) A third start after nothing happened says nothing.
    third = Notifier(LocalClient(runtime), send, state_path=state_path, clock=Clock(at))
    third.prime()
    assert _run(third.poll()) == []

    # 5) Corrupt file: fall back to priming on the current state, no crash, no replay.
    state_path.write_text("{not json")
    fourth = Notifier(LocalClient(runtime), send, state_path=state_path, clock=Clock(at))
    fourth.prime()
    assert fourth.watcher.loaded_from_disk is False and _run(fourth.poll()) == []


def test_notifier_does_not_replay_history_after_runtime_restart() -> None:
    engines = {name: make_engine("sqlite://") for name in ("shared", "baseline", "ai")}
    for engine in engines.values():
        init_db(engine)
    feed = ScriptedFeed()
    first, _ = _runtime(feed, engines=engines)
    at = _fill_one_lot(first, feed, NOW)
    second, _ = _runtime(feed, engines=engines)
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    notifier = Notifier(LocalClient(second), send, clock=Clock(at))
    at = at + timedelta(seconds=5)
    feed.set_book(_book("98.90", "98.95", at))
    second.tick(at)
    assert _run(notifier.poll()) == []
    at = at + timedelta(seconds=5)
    feed.set_book(_book("99.60", "99.65", at))
    second.tick(at)
    delivered = _run(notifier.poll())
    assert len(delivered) == 1 and "Продано BTC" in delivered[0] and "Чистая прибыль" in delivered[0]


# ---------------------------------------------------------------- aiogram wiring (no network)


class FakeSession:
    """Captures Bot API calls. Implemented against aiogram's BaseSession contract."""

    def __init__(self) -> None:
        from aiogram.client.session.base import BaseSession

        class _Session(BaseSession):
            def __init__(inner) -> None:  # noqa: N805
                super().__init__()
                inner.calls: list[Any] = []

            async def close(inner) -> None:  # noqa: N805
                return None

            async def make_request(inner, bot: Any, method: Any, timeout: int | None = None) -> Any:  # noqa: N805
                inner.calls.append(method)
                return True

            async def stream_content(inner, *args: Any, **kwargs: Any):  # noqa: N805
                yield b""

        self.session = _Session()

    @property
    def calls(self) -> list[Any]:
        return self.session.calls


def _update(update_id: int, *, user_id: int, text: str | None = None, callback: str | None = None, chat_type: str = "private") -> Any:
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=user_id, is_bot=False, first_name="t")
    chat = Chat(id=user_id if chat_type == "private" else -user_id, type=chat_type)
    message = Message(message_id=update_id, date=datetime.now(timezone.utc), chat=chat, from_user=user, text=text or "")
    if callback is None:
        return Update(update_id=update_id, message=message)
    return Update(update_id=update_id, callback_query=CallbackQuery(id=str(update_id), from_user=user, chat_instance="x", data=callback, message=message))


def test_aiogram_dispatch_routes_commands_blocks_strangers_and_groups(tmp_path: Path) -> None:
    from aiogram.methods import AnswerCallbackQuery, SendMessage

    from app.telegram.bot import TelegramApp, notification_targets

    feed = ScriptedFeed()
    runtime, _ = _runtime(
        feed, telegram_bot_token="42:local-test-token", telegram_allowed_user_ids=f"{OWNER}, 3003", telegram_state_path=str(tmp_path / "tg.json")
    )
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    fake = FakeSession()
    app = TelegramApp(runtime, runtime.settings, session=fake.session)
    assert notification_targets(runtime.settings) == [OWNER, 3003]
    assert app.service.controls_enabled is False

    async def scenario() -> None:
        await app.dispatcher.feed_update(app.bot, _update(1, user_id=OWNER, text="/balance"))
        await app.dispatcher.feed_update(app.bot, _update(2, user_id=STRANGER, text="/balance"))
        await app.dispatcher.feed_update(app.bot, _update(3, user_id=STRANGER, text="/halt"))
        await app.dispatcher.feed_update(app.bot, _update(4, user_id=OWNER, callback="cmd:status"))
        await app.dispatcher.feed_update(app.bot, _update(5, user_id=STRANGER, callback="cmd:halt"))
        await app.dispatcher.feed_update(app.bot, _update(6, user_id=OWNER, text="/halt"))
        await app.dispatcher.feed_update(app.bot, _update(7, user_id=OWNER, text="/balance", chat_type="group"))
        await app.dispatcher.feed_update(app.bot, _update(8, user_id=STRANGER, text="/whoami"))
        await app.send_test()

    _run(scenario())
    sends = [c for c in fake.calls if isinstance(c, SendMessage)]
    assert [c.chat_id for c in sends] == [OWNER, STRANGER, OWNER, OWNER, STRANGER, OWNER, 3003]
    assert "Баланс" in sends[0].text and app.bot.default.parse_mode == "HTML"
    assert sends[1].text == "Доступ запрещён."
    assert "Лента" in sends[2].text
    assert "только читает" in sends[3].text
    assert sends[4].text == f"Ваш Telegram user id: <code>{STRANGER}</code>"
    assert sends[5].text.startswith("✅ Тестовое уведомление") and "только чтение" in sends[5].text
    assert runtime.controls.halt_all is False
    assert sum(isinstance(c, AnswerCallbackQuery) for c in fake.calls) == 2
    assert "local-test-token" not in " ".join(str(c) for c in sends)
    _run(app.bot.session.close())


def test_single_polling_instance_per_token(tmp_path: Path) -> None:
    from app.telegram.bot import PollingLock, lock_path_for

    settings = Settings(telegram_bot_token="42:tok", telegram_allowed_user_ids="1", telegram_state_path=str(tmp_path / "s.json"))  # type: ignore[arg-type]
    path = lock_path_for(settings)
    assert path.parent == tmp_path and "42:tok" not in path.name
    first, second = PollingLock(path), PollingLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.unlock()
    assert second.acquire() is True
    second.unlock()


def test_api_process_does_not_poll_telegram_unless_told_to(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    settings = Settings(exchange="simulated", telegram_bot_token="", telegram_allowed_user_ids="1")  # type: ignore[arg-type]
    assert settings.telegram_enabled is False
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["telegram"] is False and health["live_trading"] is False
    # Token + allowlist set, default mode "process": the API still does not start a poller
    # unless this process is the Render paper role.
    configured = Settings(exchange="simulated", telegram_bot_token="42:tok", telegram_allowed_user_ids="1")  # type: ignore[arg-type]
    assert configured.telegram_enabled is True and configured.telegram_inprocess is False
    app = create_app(configured)
    with TestClient(app) as client:
        assert client.get("/health").json()["telegram"] is False
    with pytest.raises(ValueError):
        Settings(telegram_allowed_user_ids="abc")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(telegram_digest_interval_sec=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(telegram_api_base_url="http://0.0.0.0:8000")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(telegram_drawdown_alert_pct="0")  # type: ignore[arg-type]
    assert Settings(telegram_allowed_user_ids="7;8, 9").telegram_user_ids == frozenset({7, 8, 9})  # type: ignore[arg-type]


def test_telegram_entrypoint_refuses_to_start_unconfigured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.telegram.__main__ import main

    for key in ("TELEGRAM_BOT_TOKEN", "ATS_TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS", "ATS_TELEGRAM_ALLOWED_USER_IDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    assert main([]) == 2
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "42:tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("ATS_TELEGRAM_MODE", "inprocess")
    assert main([]) == 2
