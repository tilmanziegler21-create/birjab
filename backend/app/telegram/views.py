"""Text screens for Telegram over the /api/state JSON. HTML; every untrusted string is escaped."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import ROUND_HALF_EVEN, Decimal
from html import escape
from typing import Any

from app.market.regime import label as regime_label

ARMS = ("baseline", "ai")
ARM_LABEL = {"baseline": "Сетка", "ai": "Сетка + AI"}
_TWO = Decimal("0.01")
_FOUR = Decimal("0.0001")


def dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def money(value: Any, digits: Decimal = _TWO) -> str:
    number = dec(value)
    if number is None:
        return "—"
    quantized = number.quantize(digits, rounding=ROUND_HALF_EVEN)
    return f"{quantized:,.{abs(digits.as_tuple().exponent)}f}".replace(",", " ")


def signed(value: Any, digits: Decimal = _TWO) -> str:
    number = dec(value)
    if number is None:
        return "—"
    return ("+" if number > 0 else "") + money(number, digits)


def price(value: Any) -> str:
    number = dec(value)
    if number is None:
        return "—"
    if number >= 1000:
        return money(number, _TWO)
    if number >= 1:
        return money(number, _FOUR)
    return f"{number.normalize():f}"


def qty(value: Any) -> str:
    number = dec(value)
    return "—" if number is None else f"{number.normalize():f}"


def when(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age(now: datetime, then: Any) -> str:
    moment = when(then)
    if moment is None:
        return "никогда"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds} с назад"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин назад"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


EUROPE = ZoneInfo("Europe/Berlin")


def europe(value: Any) -> str:
    moment = when(value)
    if moment is None:
        return "—"
    local = moment.astimezone(EUROPE)
    return local.strftime("%d.%m.%Y %H:%M:%S %Z")


# ---------------------------------------------------------------- helpers over the state


def quote(state: dict[str, Any]) -> str:
    return str(state.get("quote_currency", ""))


def arm(state: dict[str, Any], name: str) -> dict[str, Any]:
    return state["arms"][name]


def news_source_label(state: dict[str, Any]) -> str:
    source = str(state["news"].get("source") or "")
    if "Mock" in source or not source:
        return "MOCK (реальные новости и LLM не подключены)"
    return escape(source)


def market_source_label(state: dict[str, Any]) -> str:
    exchange = str(state.get("exchange") or "")
    if exchange == "binance_public":
        return "Binance Public REST (только GET, без ключа)"
    if exchange == "simulated":
        return "синтетическая лента (не рынок)"
    return escape(exchange or "неизвестно")


def header(state: dict[str, Any]) -> str:
    live = "live отключён" if state.get("live_trading") is False else "⚠️ live флаг не false"
    return f"<b>AI Trading Control</b> · {escape(str(state.get('mode', '')).upper())} · {live}"


def loop_state(state: dict[str, Any], now: datetime) -> str:
    runtime = state["runtime"]
    controls = state["controls"]
    last = when(runtime.get("last_tick_at"))
    if last is None:
        return "⏳ рантайм ещё не сделал ни одного тика"
    lag = (now - last).total_seconds()
    limit = int(state["feed"].get("poll_interval_sec", 5)) * 6
    if controls.get("halt_all"):
        return "⛔ HALT: новых заявок нет, отдыхающие покупки сняты"
    if controls.get("pause_new_buys"):
        return "⏸ Пауза покупок, выходы работают"
    if lag > limit:
        return f"⚠️ OFFLINE? последний тик {age(now, last)} — цикл, похоже, стоит"
    feed_ok = sum(1 for s in state["feed"]["symbols"].values() if s.get("ok"))
    total = len(state["feed"]["symbols"])
    if total and feed_ok < total:
        return f"🟡 Работает, лента {feed_ok}/{total}"
    return "🟢 Торговая система работает"


def day_change(report: dict[str, Any], start: Any) -> str:
    equity, begin = dec(report.get("equity")), dec(start)
    if equity is None or begin is None:
        return "—"
    return signed(equity - begin)


def active_coins(state: dict[str, Any]) -> str:
    symbols = sorted(set(arm(state, "baseline")["active_symbols"]) | set(arm(state, "ai")["active_symbols"]))
    return " · ".join(s.split("/")[0] for s in symbols) or "нет"


def regimes_short(state: dict[str, Any]) -> str:
    regimes = state["news"].get("regimes", {})
    abnormal = {k: v for k, v in regimes.items() if v != "NORMAL"}
    return "NORMAL" if not abnormal else ", ".join(f"{k} {v}" for k, v in abnormal.items())


def nearest_buys(state: dict[str, Any], name: str) -> list[tuple[str, Decimal, Decimal, Decimal | None]]:
    """(symbol, limit, bid, distance %) for resting buys; distance = how far bid is above the limit."""

    out = []
    feed = state["feed"]["symbols"]
    for w in arm(state, name)["working"]:
        if w["side"] != "buy" or w["state"] not in {"OPEN", "PARTIAL"}:
            continue
        limit = dec(w["limit_price"])
        bid = dec(feed.get(w["symbol"], {}).get("bid"))
        distance = ((bid - limit) / bid * 100) if (bid and limit) else None
        out.append((w["symbol"], limit, bid, distance))  # type: ignore[arg-type]
    return out


def nearest_sells(state: dict[str, Any], name: str) -> list[tuple[str, Decimal, Decimal, Decimal | None]]:
    out = []
    feed = state["feed"]["symbols"]
    for w in arm(state, name)["working"]:
        if w["side"] != "sell" or w["state"] not in {"OPEN", "PARTIAL"}:
            continue
        limit = dec(w["limit_price"])
        bid = dec(feed.get(w["symbol"], {}).get("bid"))
        distance = ((limit - bid) / bid * 100) if (bid and limit) else None
        out.append((w["symbol"], limit, bid, distance))  # type: ignore[arg-type]
    return out


def _latest_decision(state: dict[str, Any], symbol: str, arm_name: str) -> dict[str, Any] | None:
    latest = None
    for item in state.get("decisions") or []:
        if item.get("arm") == arm_name and item.get("symbol") == symbol and item.get("side") == "buy":
            latest = item
    return latest


def pair_idle(state: dict[str, Any], symbol: str, *, arm_name: str = "baseline") -> dict[str, Any]:
    """Fact per pair. A global reject counter is not attributed to every coin."""

    feed = state["feed"]["symbols"].get(symbol) or {}
    report = arm(state, arm_name)
    buy = next((row for row in nearest_buys(state, arm_name) if row[0] == symbol), None)
    sell = next((row for row in nearest_sells(state, arm_name) if row[0] == symbol), None)
    decision = _latest_decision(state, symbol, arm_name)
    skips = (report.get("strategy_skips") or {}).get(symbol) or []
    free = dec(report.get("free_quote"))
    reserved = dec(report.get("reserved_quote"))
    if not feed.get("ok", True):
        title = "лента недоступна"
        detail = str(feed.get("error") or "нет котировки")
    elif feed.get("stale"):
        title = "устаревшие данные"
        detail = f"стакан {europe(feed.get('updated_at') or feed.get('checked_at'))}"
    elif buy is not None:
        need = buy[3]
        title = f"ожидаем снижения на {money(need, _TWO)}%" if need is not None else "ожидаем снижения до лимита"
        detail = f"лимитная заявка размещена · BUY {price(buy[1])} · сейчас {price(buy[2])}"
    elif sell is not None:
        title = "ждём выход"
        detail = f"лимитная продажа {price(sell[1])} · до SELL {signed(sell[3])}%"
    elif decision is not None and not decision.get("allowed"):
        reason = str(decision.get("reason") or "")
        if reason == "max_active_pairs":
            title = "заблокировано"
            detail = "достигнут лимит активных пар"
        elif reason in {"out_of_funds", "insufficient_free"}:
            title = "нет свободного капитала"
            detail = f"free {money(free)} · reserved {money(reserved)}"
        elif reason.startswith("stale"):
            title = "устаревшие данные"
            detail = reason
        else:
            title = "отказ RiskManager"
            detail = reason
    elif skips:
        title = "стратегия не ставит вход"
        detail = ", ".join(str(item) for item in skips)
    elif free is not None and free <= 0:
        title = "нет свободного капитала"
        detail = f"free {money(free)} · reserved {money(reserved)}"
    else:
        title = "заявки нет"
        detail = "в снимке нет resting BUY и нет свежего отказа по этой паре"
    waiting = buy is not None
    blocked = bool(decision is not None and not decision.get("allowed") and buy is None)
    row = {
        "symbol": symbol,
        "base": symbol.split("/")[0],
        "title": title,
        "detail": detail,
        "waiting": waiting,
        "blocked": blocked,
        "need_pct": buy[3] if buy is not None else None,
        "limit": buy[1] if buy is not None else None,
        "bid": buy[2] if buy is not None else dec(feed.get("bid")),
        "raw_reason": str(decision.get("reason") or "") if decision is not None else "",
    }
    row["human"] = _human_idle(row, state)
    return row


def _human_idle(row: dict[str, Any], state: dict[str, Any]) -> str:
    if row["waiting"]:
        need = row.get("need_pct")
        if need is not None:
            return f"цена ещё не дошла до входа, осталось {money(need, _TWO)}% вниз"
        return "ждём снижения до уровня входа"
    if row["blocked"] and (row.get("raw_reason") == "max_active_pairs" or "лимит" in row["detail"]):
        used = len(arm(state, "baseline").get("active_symbols") or [])
        if used:
            return f"вход не выставлен, достигнут лимит активных монет ({used} из {used})"
        return "вход не выставлен, достигнут лимит активных монет"
    if row["title"] == "нет свободного капитала":
        return "нет свободных денег для новой заявки"
    if row["title"] == "устаревшие данные":
        return "котировка устарела, новый вход не ставится"
    if row["title"] == "лента недоступна":
        return f"нет котировки: {row['detail']}"
    if row["title"] == "ждём выход":
        return row["detail"]
    return row["detail"]


def doing_now(state: dict[str, Any]) -> str:
    rows = pair_idles(state)
    waiting = [row["base"] for row in rows if row["waiting"]]
    held: list[str] = []
    for lot in arm(state, "baseline").get("lots") or []:
        if (dec(lot.get("remaining_base_qty")) or 0) > 0:
            base = str(lot["symbol"]).split("/")[0]
            if base not in held:
                held.append(base)
    if held and waiting:
        return f"Держит {', '.join(held)}. Ждёт отката для входа по {', '.join(waiting)}."
    if waiting:
        return f"Ждёт отката для входа по {', '.join(waiting)}."
    if held:
        return f"Держит позицию по {', '.join(held)}."
    if any(row["blocked"] for row in rows):
        return "Новых входов нет."
    return "Сейчас нет активных заявок."


def why_home_lines(state: dict[str, Any]) -> list[str]:
    rows = pair_idles(state)
    lines = []
    if any(row["waiting"] for row in rows):
        lines.append("Рынок выше точек входа.")
    for row in rows:
        if row["blocked"]:
            lines.append(f"{row['base']}: {row['human']}")
    return lines or ["Отдельной блокировки нет."]


def market_short(state: dict[str, Any]) -> str:
    exchange = str(state.get("exchange") or "")
    if exchange == "binance_public":
        return "Binance Public"
    if exchange == "simulated":
        return "учебная лента"
    return escape(exchange or "неизвестно")


def mode_line(state: dict[str, Any]) -> str:
    live = "live отключён" if state.get("live_trading") is False else "⚠️ live флаг не false"
    return f"Режим: {escape(str(state.get('mode', '')).upper())} · {live}"


def closed_trades(state: dict[str, Any], name: str = "baseline") -> list[dict[str, Any]]:
    report = arm(state, name)
    fills_by_lot: dict[str, list[dict[str, Any]]] = {}
    for fill in report.get("fills") or []:
        fills_by_lot.setdefault(str(fill.get("lot_id")), []).append(fill)
    out = []
    for lot in report.get("lots") or []:
        if lot.get("status") != "closed":
            continue
        related = fills_by_lot.get(str(lot.get("lot_id")), [])
        buys = [item for item in related if item.get("side") == "buy"]
        sells = [item for item in related if item.get("side") == "sell"]
        fee = sum((dec(item.get("fee_amount")) or Decimal("0") for item in related), Decimal("0"))
        out.append(
            {
                "symbol": lot["symbol"],
                "base": str(lot["symbol"]).split("/")[0],
                "buy_price": buys[0]["price"] if buys else lot.get("entry_price"),
                "sell_price": sells[-1]["price"] if sells else None,
                "pnl": dec(lot.get("realized_pnl")),
                "fee": fee,
            }
        )
    return out


def lot_unreal(state: dict[str, Any], lot: dict[str, Any]) -> Decimal | None:
    bid = dec(state["feed"]["symbols"].get(lot["symbol"], {}).get("bid")) or dec(lot.get("mark_bid"))
    qty_left = dec(lot.get("remaining_base_qty")) or Decimal("0")
    cost = dec(lot.get("quote_cost_remaining"))
    if bid is None or cost is None:
        return None
    gross = bid * qty_left
    fee_pct = dec(state.get("exit_fee_pct")) or Decimal("0.10")
    return gross - gross * fee_pct / 100 - cost


def pair_idles(state: dict[str, Any], *, arm_name: str = "baseline") -> list[dict[str, Any]]:
    symbols = list(state["feed"]["symbols"])
    extra = [row[0] for row in nearest_buys(state, arm_name) if row[0] not in symbols]
    return [pair_idle(state, symbol, arm_name=arm_name) for symbol in symbols + extra]


def idle_reasons(state: dict[str, Any]) -> list[str]:
    lines = []
    for row in pair_idles(state):
        lines.append(f"{row['base']} — {row['title']}. {row['detail']}")
    if not lines:
        lines.append("в снимке нет пар")
    return lines


def idle_headline(state: dict[str, Any]) -> str:
    rows = pair_idles(state)
    waiting = [row for row in rows if row["waiting"]]
    blocked = [row for row in rows if row["blocked"]]
    parts = []
    if waiting:
        parts.append(
            ", ".join(
                f"{row['base']} −{money(row['need_pct'], _TWO)}%" if row["need_pct"] is not None else row["base"]
                for row in waiting
            )
            + " ждут снижения"
        )
    if blocked:
        parts.append(", ".join(f"{row['base']}: {row['title']}" for row in blocked))
    return "; ".join(parts) if parts else "см. разбивку по парам"


# ---------------------------------------------------------------- screens


def total_result(report: dict[str, Any]) -> Decimal | None:
    realized = dec(report.get("realized_pnl"))
    unreal = dec(report.get("unrealized_pnl"))
    if realized is None and unreal is None:
        return None
    return (realized or Decimal("0")) + (unreal or Decimal("0"))


def waiting_budget(state: dict[str, Any], symbol: str, name: str = "baseline") -> Decimal | None:
    for item in arm(state, name).get("working") or []:
        if item.get("symbol") == symbol and item.get("side") == "buy" and item.get("state") in {"OPEN", "PARTIAL"}:
            return dec(item.get("quote_budget"))
    return None


def sell_target(state: dict[str, Any], lot_id: str, name: str = "baseline") -> Decimal | None:
    for item in arm(state, name).get("working") or []:
        if item.get("lot_id") == lot_id and item.get("side") == "sell" and item.get("state") in {"OPEN", "PARTIAL"}:
            return dec(item.get("limit_price"))
    return None


def buy_notice(state: dict[str, Any], fill: dict[str, Any], *, name: str = "baseline") -> str:
    q = quote(state)
    base = str(fill.get("symbol") or "").split("/")[0]
    target = sell_target(state, str(fill.get("lot_id") or ""), name)
    return (
        f"🟢 Куплено {escape(base)} на {money(fill.get('quote_amount'))} {q} по {price(fill.get('price'))}.\n"
        f"Цель продажи: {price(target)}.\n"
        f"Статус: позиция открыта."
    )


def sell_notice(state: dict[str, Any], fill: dict[str, Any]) -> str:
    q = quote(state)
    base = str(fill.get("symbol") or "").split("/")[0]
    return (
        f"🔴 Продано {escape(base)} по {price(fill.get('price'))}.\n"
        f"Чистая прибыль: {signed(fill.get('quote_amount'))} {q}.\n"
        f"Статус: сделка закрыта."
    )


def resolve_symbol(state: dict[str, Any], token: str) -> str | None:
    token = (token or "").strip()
    if token in state["feed"]["symbols"]:
        return token
    for symbol in state["feed"]["symbols"]:
        if symbol.split("/")[0].upper() == token.upper():
            return symbol
    return None


def pair_card_screen(state: dict[str, Any], symbol: str, now: datetime) -> str:
    resolved = resolve_symbol(state, symbol)
    if resolved is None:
        return f"Нет такой монеты: {escape(symbol)}"
    return "\n".join(["<b>Позиция</b>", ""] + _position_block(state, resolved))


def home_screen(state: dict[str, Any], now: datetime, *, expert: bool = False, last_ok_at: datetime | None = None) -> str:
    base = arm(state, "baseline")
    q = quote(state)
    waiting = nearest_buys(state, "baseline")
    open_lots = [lot for lot in base.get("lots") or [] if (dec(lot.get("remaining_base_qty")) or 0) > 0]
    status = loop_state(state, now).replace("Торговая система работает", "Бот работает")
    if "🟢" in status:
        status = "🟢 Бот работает"
    elif status.startswith("⏳"):
        status = "⏳ Ещё не стартовал"
    elif "OFFLINE" in status or "стоит" in status:
        status = "⚠️ Нет тиков"
    lines = [
        "<b>AI Trading Control</b>",
        f"{status} · {escape(str(state.get('mode', '')).upper())}",
        "",
        "Общий капитал",
        f"<b>{money(base.get('equity'))} {q}</b>",
        "",
        "Результат",
        f"<b>{signed(total_result(base))} {q}</b>",
        "",
        "Свободно",
        f"{money(base.get('free_quote'))} {q}",
        "",
        "В заявках",
        f"{money(base.get('reserved_quote'))} {q}",
    ]
    if open_lots:
        lines += ["", "Открытые позиции"]
        for lot in open_lots[:4]:
            symbol = str(lot.get("symbol") or "")
            lines += [
                f"<b>{escape(symbol.replace('/', ' / '))}</b>",
                f"P&amp;L {signed(lot_unreal(state, lot))} {q}",
            ]
    if waiting:
        lines += ["", "Ожидают покупки"]
        for symbol, limit, _bid, dist in waiting[:4]:
            need = money(dist, _TWO) if dist is not None else "—"
            lines += [
                f"<b>{escape(symbol.replace('/', ' / '))}</b>",
                f"Купить при {price(limit)}",
                f"↓ {need}%",
            ]
    lines.append("")
    lines.append(f"Открытых позиций: {len(open_lots)} · Закрыто: {base.get('completed_cycles', 0)}")
    if expert:
        ai = arm(state, "ai")
        day = state.get("equity_24h_ago", {})
        lines += [
            "",
            f"за 24 ч {day_change(base, day.get('baseline'))} · AI {signed(total_result(ai))} · тик {state['runtime'].get('tick_count')}",
        ]
    return "\n".join(lines)


def start_screen(state: dict[str, Any], now: datetime, *, expert: bool = False) -> str:
    return home_screen(state, now, expert=expert)


def status_screen(state: dict[str, Any], now: datetime, *, expert: bool = False) -> str:
    runtime = state["runtime"]
    lines = [
        "<b>Статус</b>",
        loop_state(state, now),
        mode_line(state),
        f"Рынок: {market_short(state)}",
        f"Последний тик: <code>{europe(runtime.get('last_tick_at'))}</code>",
        "",
        "<b>Лента</b>",
    ]
    for symbol, st in state["feed"]["symbols"].items():
        if not st.get("ok"):
            lines.append(f"• {escape(symbol)}: нет котировки")
            continue
        flag = " · устарела" if st.get("stale") else ""
        lines.append(f"• {escape(symbol.split('/')[0])}: {price(st.get('bid'))}{flag}")
    if expert:
        controls = state["controls"]
        lines += [
            "",
            "<b>Эксперт</b>",
            f"Источник: {market_source_label(state)} · <code>{escape(str(state.get('exchange')))}</code>",
            f"Тиков: {runtime.get('tick_count')} · запущен {age(now, runtime.get('started_at'))} · восстановлено заявок {runtime.get('restored_orders', 0)}",
            f"pause={controls.get('pause_new_buys')} halt={controls.get('halt_all')}",
            f"Новости: {news_source_label(state)}, опрос {age(now, state['news'].get('last_ok_at'))}",
        ]
        for name in ARMS:
            top = sorted((arm(state, name).get("reject_reasons") or {}).items(), key=lambda kv: kv[1], reverse=True)[:4]
            if top:
                lines.append(f"{ARM_LABEL[name]} raw: " + ", ".join(f"{escape(k)} ×{v}" for k, v in top))
        lines += ["", report_screen(state, now)]
        errors = runtime.get("errors", [])[-3:]
        if errors:
            lines += ["", "<b>Последние ошибки</b>"]
            lines += [f"  {escape(str(e['at'])[11:19])} {escape(str(e['error'])[:120])}" for e in errors]
    return "\n".join(lines)


def balance_screen(state: dict[str, Any], now: datetime) -> str:
    day = state.get("equity_24h_ago", {})
    lines = [header(state), f"<b>Баланс</b> ({quote(state)})"]
    for name in ARMS:
        r = arm(state, name)
        if "error" in r:
            lines += ["", f"<b>{ARM_LABEL[name]}</b>: нет котировки для оценки ({escape(str(r['error']))})"]
            continue
        fees = ", ".join(f"{money(v)} {k}" for k, v in sorted(r.get("fees_by_currency", {}).items())) or "0"
        lines += [
            "",
            f"<b>{ARM_LABEL[name]}</b> · equity {money(r['equity'])}",
            f"  free {money(r['free_quote'])} · reserved {money(r['reserved_quote'])} · Profit Reserve {money(r.get('profit_reserve'))} · в позициях {money(r['gross_position_value'])} ({money(r['exposure_pct'])}%)",
            f"  realized {signed(r['realized_pnl'])} · unrealized {signed(r['unrealized_pnl'])} · комиссии {fees}",
            f"  за 24 ч {day_change(r, day.get(name))} · drawdown {money(r['drawdown_pct'])}% от пика {money(r['peak_equity'])}",
        ]
        if r.get("trading_blocked"):
            lines.append(f"  ⛔ торговля заблокирована: {escape(str(r.get('block_reason')))}")
    return "\n".join(lines)


def _position_block(state: dict[str, Any], symbol: str, name: str = "baseline") -> list[str]:
    q = quote(state)
    feed = state["feed"]["symbols"].get(symbol) or {}
    report = arm(state, name)
    open_lots = [lot for lot in report.get("lots") or [] if lot.get("symbol") == symbol and (dec(lot.get("remaining_base_qty")) or 0) > 0]
    exits = {w["lot_id"]: w for w in report.get("working") or [] if w["side"] == "sell" and w["state"] in {"OPEN", "PARTIAL"}}
    buy = next((row for row in nearest_buys(state, name) if row[0] == symbol), None)
    idle = pair_idle(state, symbol, arm_name=name)
    base = symbol.split("/")[0]
    lines = [f"<b>{escape(base)}</b>"]
    if open_lots:
        for lot in open_lots:
            bid = dec(feed.get("bid")) or dec(lot.get("mark_bid"))
            exit_order = exits.get(lot["lot_id"])
            volume = dec(lot.get("quote_cost_remaining"))
            lines += [
                "Статус: <b>Куплено</b>",
                f"Вход: {price(lot.get('entry_price'))}",
                f"Текущая цена: {price(bid)}",
                f"Цель продажи: {price(exit_order['limit_price']) if exit_order else '—'}",
                f"Объём: {money(volume)} {q}",
                f"P&amp;L: {signed(lot_unreal(state, lot))} {q}",
            ]
    elif buy is not None:
        working = next((w for w in report.get("working") or [] if w.get("symbol") == symbol and w.get("side") == "buy"), None)
        budget = working.get("quote_budget") if working else None
        lines += [
            "Статус: <b>Ждёт входа</b>",
            f"Уровень входа: {price(buy[1])}",
            f"Текущая цена: {price(buy[2])}",
            f"До входа: {signed(None if buy[3] is None else -buy[3])}%",
            f"Сумма: {money(budget)} {q}" if budget is not None else "Сумма: —",
        ]
    else:
        lines += ["Статус: <b>Неактивно</b>", idle["human"]]
    return lines


def positions_screen(state: dict[str, Any], now: datetime, *, expert: bool = False) -> str:
    names = ARMS if expert else ("baseline",)
    lines = ["<b>Позиции</b>"]
    if expert:
        lines.append("Сетка и сетка + AI по отдельности.")
    for name in names:
        if expert:
            lines += ["", f"<b>{ARM_LABEL[name]}</b>"]
        symbols = list(state["feed"]["symbols"])
        extra = [row[0] for row in nearest_buys(state, name) if row[0] not in symbols]
        for symbol in symbols + extra:
            lines += [""] + _position_block(state, symbol, name)
    return "\n".join(lines)


def _trades_block(state: dict[str, Any], name: str, *, expert: bool, limit: int) -> list[str]:
    q = quote(state)
    closed = closed_trades(state, name)
    total = sum((item["pnl"] or Decimal("0") for item in closed), Decimal("0"))
    wins = sum(1 for item in closed if (item["pnl"] or 0) > 0)
    avg = (total / Decimal(len(closed))) if closed else None
    win_pct = (Decimal(wins) / Decimal(len(closed)) * Decimal("100")) if closed else None
    lines = []
    if not closed:
        lines.append("Пока нет закрытых сделок.")
    for index, item in enumerate(closed[-limit:][::-1], start=1):
        lines += [
            f"<b>{index}. {escape(item['base'])}</b>",
            f"Купил: {price(item['buy_price'])}",
            f"Продал: {price(item['sell_price'])}",
            f"Прибыль: {signed(item['pnl'])} {q}",
            f"Комиссия: {money(item['fee'], _FOUR)} {q}",
            "",
        ]
    lines += [
        "<b>Сводка</b>",
        f"Всего закрытых сделок: {len(closed)}",
        f"Суммарная прибыль: {signed(total)} {q}",
        f"Средняя прибыль на сделку: {signed(avg)} {q}",
        f"Прибыльных: {money(win_pct, _TWO)}%" if win_pct is not None else "Прибыльных: —",
    ]
    if expert:
        fills = arm(state, name).get("fills") or []
        lines += ["", "Служебно, последние исполнения:"]
        if not fills:
            lines.append("пока нет")
        for fill in fills[-limit:][::-1]:
            if fill["side"] == "buy":
                lines.append(f"BUY {fill['symbol']} {qty(fill['quantity'])} @ {price(fill['price'])}, комиссия {money(fill['fee_amount'], _FOUR)}")
            else:
                lines.append(f"SELL {fill['symbol']} {qty(fill['quantity'])} @ {price(fill['price'])}, чистый результат {signed(fill['quote_amount'], _FOUR)}")
    return lines


def trades_screen(state: dict[str, Any], now: datetime, limit: int = 8, *, expert: bool = False) -> str:
    lines = ["<b>Последние сделки</b>"]
    names = ARMS if expert else ("baseline",)
    for name in names:
        if expert:
            lines += ["", f"<b>{ARM_LABEL[name]}</b>"]
        lines += _trades_block(state, name, expert=expert, limit=limit)
    return "\n".join(lines)


def news_screen(state: dict[str, Any], now: datetime, limit: int = 3, *, expert: bool = False) -> str:
    view = state["news"]
    source = news_source_label(state)
    lines = ["<b>AI / Новости</b>"]
    if "MOCK" in source:
        lines.append("Источник: MOCK. Реальные новости и LLM пока не подключены.")
    else:
        lines.append(f"Источник: {source}")
    regimes = view.get("regimes") or {}
    abnormal = {k: v for k, v in regimes.items() if v != "NORMAL"}
    lines.append("Режим новостей: " + (", ".join(f"{k} {v}" for k, v in abnormal.items()) if abnormal else "обычный"))
    vetoes = (state.get("ab") or {}).get("ai_vetoes") or []
    news_blocks = [item for item in vetoes if str(item.get("reason") or "").startswith("news_")]
    if news_blocks:
        lines.append("Блокировки AI: есть, новые покупки по затронутым монетам не ставятся.")
    else:
        lines.append("Блокировок от AI нет.")
    events = view.get("events", [])[:limit]
    lines += ["", "<b>Последние события</b>"]
    if not events:
        lines.append("Событий нет.")
    for event in events:
        lines.append(f"• {escape(str(event.get('headline') or '')[:160])}")
    if expert:
        lines += [
            "",
            f"Вызовов mock-классификатора {view.get('analyst_calls')} (не LLM)",
            f"Последний удачный опрос: {age(now, view.get('last_ok_at'))}",
            "Режимы: " + ", ".join(f"{base} {regime}" for base, regime in regimes.items()),
        ]
        for event in view.get("events", [])[:5]:
            kind = "TEST/MOCK" if event.get("illustrative") else "неподтверждено"
            links = event.get("links") or []
            link = escape(str(links[0])) if links else "—"
            lines += [
                "",
                f"<b>{escape(str(event['regime']))}</b> · {kind} · {', '.join(event.get('symbols') or [])}",
                f"  {escape(str(event['headline'])[:200])}",
                f"  источник: {escape(', '.join(event.get('sources') or []) or '—')}",
                f"  ссылка: {link}",
                f"  вывод (mock-классификатор, не LLM): {escape(str(event['regime']))}",
            ]
        if vetoes:
            lines += ["", "<b>Последние вето фильтра</b> (mock-аналитик, не настоящий ИИ)"]
            for veto in vetoes[-3:]:
                lines.append(f"  {veto['symbol']} · {escape(str(veto['reason']))} · {veto.get('verdict') or 'открыто'}")
    return "\n".join(lines)


def report_screen(state: dict[str, Any], now: datetime) -> str:
    ab = state["ab"]
    base, ai = arm(state, "baseline"), arm(state, "ai")
    runtime = state["runtime"]
    lines = [
        header(state),
        "<b>A/B: сетка против сетки с AI-фильтром</b>",
        f"Baseline equity {money(ab['baseline_equity'])} · AI equity {money(ab['ai_equity'])} · Δ(AI−baseline) {signed(ab['equity_delta'])}",
        f"Циклы: {base['completed_cycles']} / {ai['completed_cycles']} · открытые лоты {base['open_lots']} / {ai['open_lots']}",
        f"Realized: {signed(base.get('realized_pnl'))} / {signed(ai.get('realized_pnl'))} · drawdown {money(base.get('drawdown_pct'))}% / {money(ai.get('drawdown_pct'))}%",
        f"Входы разрешено/запрещено: {base['entries_allowed']}/{base['entries_rejected']} · {ai['entries_allowed']}/{ai['entries_rejected']}",
        f"Вето AI: {len(ab.get('ai_vetoes', []))} (спасло {ab['saved']}, упустило {ab['missed']}) · независимых событий {ab['independent_events']}",
    ]
    for name, r in (("baseline", base), ("ai", ai)):
        top = sorted(r.get("reject_reasons", {}).items(), key=lambda kv: kv[1], reverse=True)[:3]
        if top:
            lines.append(f"Причины отказов {ARM_LABEL[name]}: " + ", ".join(f"{escape(k)} ×{v}" for k, v in top))
    started = when(runtime.get("started_at"))
    uptime = int((now - started).total_seconds()) if started else 0
    lines += [
        "",
        f"Процесс работает {max(uptime, 0) // 3600} ч {max(uptime, 0) % 3600 // 60} мин · тиков {runtime.get('tick_count')} · ошибок {len(runtime.get('errors', []))}",
        f"Новости: {news_source_label(state)}. Это проверка исправности, не прогноз доходности.",
    ]
    return "\n".join(lines)


def live_screen(
    state: dict[str, Any],
    now: datetime,
    *,
    changes: dict[str, dict[str, Any]] | None = None,
    regimes: dict[str, Any] | None = None,
    last_ok_at: datetime | None = None,
) -> str:
    """Live panel from the real /api/state feed. Missing fields stay '—'; nothing is invented."""

    base = arm(state, "baseline")
    q = quote(state)
    lines = [
        header(state),
        loop_state(state, now),
        f"Сейчас (Europe/Berlin): <code>{europe(now)}</code>",
        f"Последнее успешное обновление панели: <code>{europe(last_ok_at or now)}</code>",
        f"Тик движка: {state['runtime'].get('tick_count')} · {europe(state['runtime'].get('last_tick_at'))}",
        f"Источник рынка: {market_source_label(state)} · {escape(str(state.get('exchange')))}",
        "",
        f"<b>Сетка</b> equity {money(base.get('equity'))} {q} · realized {signed(base.get('realized_pnl'))} · unrealized {signed(base.get('unrealized_pnl'))}",
        f"free {money(base.get('free_quote'))} · reserved {money(base.get('reserved_quote'))} · Reserve {money(base.get('profit_reserve'))}",
        f"лоты {base.get('open_lots')} · циклы {base.get('completed_cycles')}",
        "",
        "<b>Рынок</b>",
    ]
    feed = state["feed"]["symbols"]
    buys = {s: (limit, dist) for s, limit, _b, dist in nearest_buys(state, "baseline")}
    sells = {s: (limit, dist) for s, limit, _b, dist in nearest_sells(state, "baseline")}
    for symbol, st in feed.items():
        if not st.get("ok"):
            lines.append(f"{symbol}: нет котировки · {escape(str(st.get('error') or '—'))}")
            continue
        bid = st.get("bid")
        ch = (changes or {}).get(symbol) or {}
        d5 = signed(ch.get("m5"), _TWO) if ch.get("m5") is not None else "—"
        d1h = signed(ch.get("h1"), _TWO) if ch.get("h1") is not None else "—"
        d1d = signed(ch.get("d1"), _TWO) if ch.get("d1") is not None else "—"
        buy = buys.get(symbol)
        sell = sells.get(symbol)
        buy_dist = None if buy is None or buy[1] is None else -buy[1]
        buy_txt = f"BUY {price(buy[0])} · до входа {signed(buy_dist)}%" if buy else "BUY —"
        sell_txt = f"SELL {price(sell[0])} ({signed(sell[1])}%)" if sell else "SELL —"
        raw_regime = (regimes or {}).get(symbol) or (regimes or {}).get(symbol.split("/")[0])
        status = pair_idle(state, symbol)
        lines += [
            f"<b>{symbol}</b>  {price(bid)} {q} · Δ5м {d5}% · Δ1ч {d1h}% · Δ24ч {d1d}%",
            f"  {buy_txt} · {sell_txt}",
            f"  {escape(status['title'])}: {escape(status['detail'])}",
            f"  режим {escape(regime_label(raw_regime))}",
            f"  стакан {europe(st.get('updated_at') or st.get('checked_at'))}",
        ]
    feed_times = [st.get("updated_at") or st.get("checked_at") for st in feed.values() if st.get("ok")]
    last_feed = max((when(item) for item in feed_times if when(item) is not None), default=None)
    lines += [
        "",
        f"Последний тик движка: <code>{europe(state['runtime'].get('last_tick_at'))}</code> · № {state['runtime'].get('tick_count')}",
        f"Последние котировки: <code>{europe(last_feed)}</code>",
        f"<b>Почему нет сделок</b>: {escape(idle_headline(state))}",
        "Разбивка по монетам выше. max_active_pairs относится только к парам без заявки.",
    ]
    return "\n".join(lines)


def why_idle_screen(state: dict[str, Any], now: datetime, *, expert: bool = False) -> str:
    lines = ["<b>Почему сейчас нет новых сделок</b>", ""]
    for row in pair_idles(state):
        lines.append(f"• <b>{escape(row['base'])}:</b> {escape(row['human'])}")
    lines += ["", "<i>Бот покупает только после отката. Это не ошибка.</i>"]
    if expert:
        lines += ["", "<b>Служебно</b>"]
        for name in ARMS:
            top = sorted((arm(state, name).get("reject_reasons") or {}).items(), key=lambda kv: kv[1], reverse=True)[:4]
            if top:
                lines.append(f"{ARM_LABEL[name]}: " + ", ".join(f"{escape(k)} ×{v}" for k, v in top))
        for row in pair_idles(state):
            if row.get("raw_reason"):
                lines.append(f"{row['base']} raw: {escape(row['raw_reason'])}")
    return "\n".join(lines)


def digest_screen(state: dict[str, Any], now: datetime, suppressed: dict[str, int]) -> str:
    base, ai = arm(state, "baseline"), arm(state, "ai")
    q = quote(state)
    feed = state["feed"]["symbols"]
    lines = [
        f"<b>Сводка</b> · {escape(str(state.get('mode', '')).upper())}",
        f"Equity {money(base.get('equity'))} / {money(ai.get('equity'))} {q} · циклы {base['completed_cycles']} / {ai['completed_cycles']} · лоты {base['open_lots']} / {ai['open_lots']}",
        f"Realized {signed(base.get('realized_pnl'))} / {signed(ai.get('realized_pnl'))} · unrealized {signed(base.get('unrealized_pnl'))} / {signed(ai.get('unrealized_pnl'))}",
        f"Лента: {sum(1 for s in feed.values() if s.get('ok'))}/{len(feed)} ок · тиков {state['runtime'].get('tick_count')} · ошибок {len(state['runtime'].get('errors', []))}",
    ]
    buys = nearest_buys(state, "baseline")
    if buys:
        lines.append("До входа: " + ", ".join(f"{s.split('/')[0]} {money(d, _TWO) if d is not None else '—'}%" for s, _l, _b, d in buys))
    paused = state.get("paused_reasons", [])
    if paused:
        lines.append("Ограничения: " + escape(", ".join(paused[:5])))
    if suppressed:
        lines.append("Подавлено уведомлений: " + ", ".join(f"{escape(k)} ×{v}" for k, v in sorted(suppressed.items())))
    return "\n".join(lines)
