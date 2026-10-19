from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.telegram.auth import Allowlist
from app.telegram.commands import CommandService
from app.telegram.dashboard import LiveDashboard
from app.telegram.views import europe, live_screen, why_idle_screen
from tests.test_telegram import OWNER, Clock, _runtime
from app.paper.feed import ScriptedFeed
from tests.test_stress import _book
from tests.conftest import NOW


def _state():
    feed = ScriptedFeed()
    runtime, _ = _runtime(feed)
    feed.set_book(_book("99.98", "100.00", NOW))
    runtime.tick(NOW)
    return runtime.snapshot(NOW), runtime


def test_live_screen_uses_real_feed_and_europe_time() -> None:
    state, _ = _state()
    text = live_screen(state, NOW, last_ok_at=NOW)
    assert "Europe/Berlin" in text and europe(NOW) in text
    assert "BTC/USDC" in text and "99.98" in text
    assert "DEMO" not in text and "84 250" not in text
    assert "Почему нет сделок" in text
    assert "Последний тик движка" in text
    assert "Последние котировки" in text
    assert "binance_public" in text or "синтетическая" in text or "scripted" in text or "simulated" in text


def test_why_command_lists_waiting_distance() -> None:
    _state_unused, runtime = _state()
    clock = Clock(NOW)
    from app.telegram.client import LocalClient

    service = CommandService(LocalClient(runtime, clock=clock), Allowlist(frozenset({OWNER})), clock=clock)
    why = service.handle_text(OWNER, "/why")
    assert why is not None and ("осталось" in why.text or "ждём снижения" in why.text)
    home = service.handle_text(OWNER, "/start")
    home_labels = [label for row in home.buttons for label, _ in row]
    assert "BTC" in home_labels
    card = service.handle_callback(OWNER, "pair:BTC/USDC")
    assert card is not None and ("Ждёт входа" in card.text or "Неактивно" in card.text or "Куплено" in card.text)
    labels = [label for row in why.buttons for label, _ in row]
    assert "Главная" in labels and "Позиции" in labels and "A/B" not in labels
    why_alias = service.handle_text(OWNER, "/why_no_trades")
    assert why_alias is not None and why_alias.text == why.text
    live = service.handle_text(OWNER, "/live")
    assert live is not None and live.edit is False
    service.handle_text(OWNER, "/expert")
    live_x = service.handle_text(OWNER, "/live")
    assert live_x is not None and "Живая панель" in "".join(label for row in live_x.buttons for label, _ in row)
    refresh = service.handle_text(OWNER, "/refresh")
    assert refresh is not None and refresh.edit is True


def test_dashboard_edits_existing_message(tmp_path: Path) -> None:
    state, runtime = _state()
    sent: list[str] = []
    edited: list[tuple[int, int, str]] = []

    async def send(chat_id: int, text: str) -> int:
        sent.append(text)
        return 11

    async def edit(chat_id: int, message_id: int, text: str) -> int:
        edited.append((chat_id, message_id, text))
        return message_id

    from app.telegram.client import LocalClient

    path = tmp_path / "tg.json"
    dash = LiveDashboard(LocalClient(runtime), state_path=path, chat_ids=[OWNER], edit=edit, send=send, interval_sec=10)
    import asyncio

    asyncio.run(dash.push())
    assert len(sent) == 1 and edited == []
    assert "Общий капитал" in sent[0] and "Результат" in sent[0] and "A/B" not in sent[0] and "equity" not in sent[0].lower()
    asyncio.run(dash.push(force=True))
    assert len(sent) == 1 and len(edited) == 1 and edited[0][1] == 11
    saved = json.loads(path.read_text())
    assert saved["live"][str(OWNER)] == 11


def test_europe_formats_berlin() -> None:
    at = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
    assert "20:00:00" in europe(at) and ("CEST" in europe(at) or "CET" in europe(at) or "+02" in europe(at))


def _waiting_state() -> dict:
    at = NOW.isoformat()
    arm = {
        "working": [
            {"symbol": "BTC/USDT", "side": "buy", "state": "OPEN", "limit_price": "83024.97", "quote_budget": "50"},
            {"symbol": "ETH/USDT", "side": "buy", "state": "OPEN", "limit_price": "2516.90", "quote_budget": "50"},
            {"symbol": "SOL/USDT", "side": "buy", "state": "OPEN", "limit_price": "110.1664", "quote_budget": "50"},
        ],
        "open_lots": 0,
        "completed_cycles": 0,
        "lots": [],
        "fills": [],
        "strategy_skips": {},
        "reject_reasons": {"max_active_pairs": 4124},
        "free_quote": "850",
        "reserved_quote": "150",
        "profit_reserve": "0",
        "equity": "1000",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "active_symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "entries_allowed": 3,
        "entries_rejected": 4124,
    }
    return {
        "quote_currency": "USDT",
        "mode": "paper",
        "live_trading": False,
        "exchange": "binance_public",
        "feed": {
            "name": "binance_public",
            "poll_interval_sec": 5,
            "symbols": {
                "BTC/USDT": {"ok": True, "stale": False, "bid": "83702", "ask": "83710", "updated_at": at, "checked_at": at},
                "ETH/USDT": {"ok": True, "stale": False, "bid": "2535.81", "ask": "2536", "updated_at": at, "checked_at": at},
                "SOL/USDT": {"ok": True, "stale": False, "bid": "111.41", "ask": "111.42", "updated_at": at, "checked_at": at},
                "XRP/USDT": {"ok": True, "stale": False, "bid": "1.4089", "ask": "1.409", "updated_at": at, "checked_at": at},
            },
        },
        "arms": {"baseline": arm, "ai": dict(arm)},
        "decisions": [
            {"arm": "baseline", "symbol": "XRP/USDT", "side": "buy", "allowed": False, "reason": "max_active_pairs"},
        ],
        "runtime": {"last_tick_at": at, "tick_count": 99, "started_at": at, "errors": []},
        "news": {"source": "MockNewsSource", "regimes": {}},
        "paused_reasons": [],
        "controls": {},
    }


def test_why_idle_is_per_pair_not_global_reject_counter() -> None:
    from app.telegram.views import idle_headline, pair_idle, why_idle_screen

    state = _waiting_state()
    btc = pair_idle(state, "BTC/USDT")
    xrp = pair_idle(state, "XRP/USDT")
    assert btc["waiting"] is True and "осталось" in btc["human"]
    assert xrp["blocked"] is True and "лимит активных монет" in xrp["human"]
    text = why_idle_screen(state, NOW)
    assert "BTC" in text and "осталось" in text
    assert "XRP" in text and "лимит активных монет" in text
    assert "max_active_pairs ×4124" not in text
    expert = why_idle_screen(state, NOW, expert=True)
    assert "max_active_pairs ×4124" in expert
    headline = idle_headline(state)
    assert "ждут снижения" in headline and "XRP" in headline
    assert "×4124" not in headline
    live = live_screen(state, NOW, last_ok_at=NOW)
    assert "Последний тик движка" in live
    assert "Последние котировки" in live
    assert "до входа" in live
    assert "не для ордеров" in live or "нет истории" in live
    from app.telegram.views import home_screen

    home = home_screen(state, NOW)
    assert "Общий капитал" in home and "1 000.00" in home
    assert "Ожидают покупки" in home and "Купить при" in home and "↓" in home
    assert "Открытых позиций: 0 · Закрыто: 0" in home
    assert "XRP: вход не выставлен" not in home
    assert "equity" not in home.lower() and "UNKNOWN" not in home and "DEMO" not in home
    from app.telegram.views import pair_card_screen

    card = pair_card_screen(state, "BTC", NOW)
    assert "Ждёт входа" in card and "83 024.97" in card and "50.00" in card
