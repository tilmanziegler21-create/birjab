"""Read-only observer for a running paper process. Samples /api/state, then writes a report.

Only GET requests. It never calls controls, never touches the databases, never starts a runtime.
Usage: python scripts/paper_watch.py --minutes 60 --out reports/PAPER_1H_REPORT.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ARMS = ("baseline", "ai")
LABEL = {"baseline": "baseline_grid", "ai": "ai_filtered_grid"}


def get(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
        return json.loads(response.read())


def dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def fmt(value, digits: str = "0.01") -> str:
    if value is None:
        return "—"
    return f"{Decimal(str(value)).quantize(Decimal(digits)):f}"


def arm_summary(state: dict, name: str) -> dict:
    arm = state["arms"][name]
    fills = arm.get("fills", [])
    lots = arm.get("lots", [])
    return {
        "equity": arm.get("equity"),
        "free": arm.get("free_quote"),
        "reserved": arm.get("reserved_quote"),
        "gross_position_value": arm.get("gross_position_value"),
        "realized_pnl": arm.get("realized_pnl"),
        "unrealized_pnl": arm.get("unrealized_pnl"),
        "fees": arm.get("fees_by_currency", {}),
        "peak_equity": arm.get("peak_equity"),
        "drawdown_pct": arm.get("drawdown_pct"),
        "exposure_pct": arm.get("exposure_pct"),
        "open_lots": arm.get("open_lots"),
        "completed_cycles": arm.get("completed_cycles"),
        "buys": sum(1 for f in fills if f["side"] == "buy"),
        "sells": sum(1 for f in fills if f["side"] == "sell"),
        "fill_ids": sorted(f["fill_id"] for f in fills),
        "fills": fills,
        "lots": lots,
        "working": arm.get("working", []),
        "active_symbols": arm.get("active_symbols", []),
        "entries_allowed": arm.get("entries_allowed"),
        "entries_rejected": arm.get("entries_rejected"),
        "reject_reasons": arm.get("reject_reasons", {}),
        "strategy_skips": arm.get("strategy_skips", {}),
        "trading_blocked": arm.get("trading_blocked"),
        "block_reason": arm.get("block_reason"),
    }


def buy_distances(state: dict) -> dict:
    """Per arm, per symbol: how far (in %) the current bid is above the nearest resting buy limit."""

    feed = state["feed"]["symbols"]
    out: dict = {}
    for name in ARMS:
        per_symbol: dict = {}
        for w in state["arms"][name].get("working", []):
            if w["side"] != "buy" or w["state"] not in {"OPEN", "PARTIAL"}:
                continue
            bid = dec(feed.get(w["symbol"], {}).get("bid"))
            limit = dec(w["limit_price"])
            if bid is None or limit is None or bid == 0:
                continue
            pct = (bid - limit) / bid * Decimal("100")
            current = per_symbol.get(w["symbol"])
            if current is None or pct < current["pct"]:
                per_symbol[w["symbol"]] = {"limit": str(limit), "bid": str(bid), "pct": str(pct.quantize(Decimal("0.0001")))}
        out[name] = per_symbol
    return out


def snapshot(base_url: str) -> dict:
    health = get(f"{base_url}/health")
    state = get(f"{base_url}/api/state")
    feed = state["feed"]["symbols"]
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "health": health,
        "ticks": state["runtime"]["tick_count"],
        "last_tick_at": state["runtime"]["last_tick_at"],
        "started_at": state["runtime"].get("started_at"),
        "restored_orders": state["runtime"].get("restored_orders"),
        "anchors": state["runtime"].get("anchors", {}),
        "buy_distance": buy_distances(state),
        "errors": state["runtime"]["errors"],
        "controls": {k: state["controls"][k] for k in ("pause_new_buys", "halt_all", "liquidate_confirmed")},
        "feed": {
            s: {
                "ok": v.get("ok"),
                "stale": v.get("stale"),
                "allowed": v.get("allowed"),
                "bid": v.get("bid"),
                "ask": v.get("ask"),
                "spread_pct": v.get("spread_pct"),
                "error": v.get("error"),
                "reasons": v.get("reasons"),
            }
            for s, v in feed.items()
        },
        "regimes": state["news"]["regimes"],
        "paused_reasons": state["paused_reasons"],
        "ab": {k: state["ab"][k] for k in ("equity_delta", "saved", "missed", "independent_events")},
        "ai_vetoes": state["ab"]["ai_vetoes"],
        "decisions": state["decisions"],
        "arms": {name: arm_summary(state, name) for name in ARMS},
    }


def light(sample: dict) -> dict:
    """Per-sample line for the jsonl log: no fill/lot bodies."""

    out = {k: v for k, v in sample.items() if k not in {"decisions", "ai_vetoes", "arms"}}
    out["arms"] = {
        name: {k: v for k, v in arm.items() if k not in {"fills", "lots", "working", "fill_ids"}} for name, arm in sample["arms"].items()
    }
    out["health"] = {k: sample["health"].get(k) for k in ("live_trading", "live_adapter", "ticks", "loop")}
    return out


def write_report(
    path: Path,
    first: dict,
    last: dict,
    samples: list[dict],
    config_path: str,
    started: datetime,
    ended: datetime,
    outages: list[str],
    *,
    title: str = "PAPER 1H REPORT",
    period_word: str = "час",
    samples_file: str = "var/watch_1h.jsonl",
    log_file: str = "var/watch_1h.log",
) -> None:
    lines: list[str] = []
    add = lines.append
    quote = "USDT"
    add(f"# {title} — реальные котировки Binance, виртуальные деньги")
    add("")
    add(f"Период наблюдения: {started.isoformat(timespec='seconds')} — {ended.isoformat(timespec='seconds')} UTC ({int((ended - started).total_seconds() // 60)} мин).")
    restarts = sorted({s["started_at"] for s in samples if s.get("started_at")} - {first.get("started_at")})
    add(
        f"Конфиг процесса: `{config_path}`. Монитор процесс не останавливал и не перезапускал; настройки, стратегия и лимиты не менялись."
        + (f" Зафиксированы рестарты торгового процесса (новый `started_at`): {', '.join(restarts)}." if restarts else " Рестартов торгового процесса за период не было.")
    )
    add(f"Режим: `{last['health'].get('mode')}`, биржа данных `{last['health'].get('exchange')}`, `live_trading={last['health'].get('live_trading')}`, `live_adapter={last['health'].get('live_adapter')}` на каждом из {len(samples)} опросов.")
    add("")
    add("## Начальное и конечное состояние")
    add("")
    add("| показатель | baseline старт | baseline финиш | AI старт | AI финиш |")
    add("| --- | --- | --- | --- | --- |")
    rows = [
        ("equity", "equity"),
        ("free USDT", "free"),
        ("reserved USDT", "reserved"),
        ("в позициях (gross)", "gross_position_value"),
        ("realized P&L", "realized_pnl"),
        ("unrealized P&L (после комиссии выхода)", "unrealized_pnl"),
        ("пик equity", "peak_equity"),
        ("drawdown %", "drawdown_pct"),
        ("exposure %", "exposure_pct"),
    ]
    for title, key in rows:
        add(f"| {title} | {fmt(first['arms']['baseline'][key])} | {fmt(last['arms']['baseline'][key])} | {fmt(first['arms']['ai'][key])} | {fmt(last['arms']['ai'][key])} |")
    for title, key in (("открытые лоты", "open_lots"), ("закрытые циклы", "completed_cycles"), ("покупки (fills)", "buys"), ("продажи (fills)", "sells")):
        add(f"| {title} | {first['arms']['baseline'][key]} | {last['arms']['baseline'][key]} | {first['arms']['ai'][key]} | {last['arms']['ai'][key]} |")
    fees_first = {n: first["arms"][n]["fees"] for n in ARMS}
    fees_last = {n: last["arms"][n]["fees"] for n in ARMS}
    add(f"| комиссии (накопл.) | {fees_first['baseline'] or 0} | {fees_last['baseline'] or 0} | {fees_first['ai'] or 0} | {fees_last['ai'] or 0} |")
    add(f"| тиков рантайма | {first['ticks']} | {last['ticks']} | | |")
    add("")
    add(f"## Итог за {period_word}")
    add("")
    for name in ARMS:
        a, b = first["arms"][name], last["arms"][name]
        d_eq = dec(b["equity"]) - dec(a["equity"]) if a["equity"] is not None and b["equity"] is not None else None
        d_real = dec(b["realized_pnl"]) - dec(a["realized_pnl"]) if a["realized_pnl"] is not None and b["realized_pnl"] is not None else None
        new_fills = [f for f in b["fills"] if f["fill_id"] not in set(a["fill_ids"])]
        buys = [f for f in new_fills if f["side"] == "buy"]
        sells = [f for f in new_fills if f["side"] == "sell"]
        fee_sum = sum((dec(f["fee_amount"]) for f in new_fills), Decimal("0"))
        add(
            f"- **{LABEL[name]}**: покупок {len(buys)}, продаж {len(sells)}, завершённых циклов за {period_word} {b['completed_cycles'] - a['completed_cycles']}, "
            f"комиссий уплачено {fmt(fee_sum, '0.0001')} {quote}, realized за {period_word} {fmt(d_real)} {quote}, изменение equity {fmt(d_eq)} {quote} "
            f"(с {fmt(a['equity'])} до {fmt(b['equity'])})."
        )
    delta = last["ab"]["equity_delta"]
    add(f"- **A/B**: Δ equity (AI − baseline) в конце {fmt(delta)} {quote}; вето AI: {len(last['ai_vetoes'])} (спасло {last['ab']['saved']}, упустило {last['ab']['missed']}); независимых новостных событий {last['ab']['independent_events']}.")
    add("")
    add(f"## Сделки за {period_word} (fills)")
    add("")
    any_fills = False
    for name in ARMS:
        known = set(first["arms"][name]["fill_ids"])
        new_fills = [f for f in last["arms"][name]["fills"] if f["fill_id"] not in known]
        if not new_fills:
            continue
        any_fills = True
        add(f"### {LABEL[name]}")
        add("")
        add("| сторона | пара | цена | количество | сумма USDT | комиссия | лот |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for f in new_fills:
            add(f"| {f['side']} | {f['symbol']} | {fmt(f['price'], '0.0001')} | {f['quantity']} | {fmt(f['quote_amount'])} | {fmt(f['fee_amount'], '0.0001')} {f['fee_currency']} | `{f['lot_id']}` |")
        add("")
    if not any_fills:
        add("Исполненных сделок за период не было.")
        add("")
    add("## Закрытые циклы (все, на конец периода)")
    add("")
    closed_any = False
    for name in ARMS:
        closed = [lot for lot in last["arms"][name]["lots"] if lot["status"] == "closed"]
        if closed:
            closed_any = True
            add(f"### {LABEL[name]}")
            add("")
            add("| лот | пара | вход | количество | realized P&L |")
            add("| --- | --- | --- | --- | --- |")
            for lot in closed:
                add(f"| `{lot['lot_id']}` | {lot['symbol']} | {fmt(lot['entry_price'], '0.0001')} | {lot['gross_base_qty']} | {fmt(lot['realized_pnl'], '0.0001')} |")
            add("")
    if not closed_any:
        add("Завершённых циклов BUY → SELL пока нет.")
        add("")
    add("## Открытые позиции и отдыхающие заявки на конец периода")
    add("")
    for name in ARMS:
        arm = last["arms"][name]
        open_lots = [lot for lot in arm["lots"] if dec(lot["remaining_base_qty"]) > 0]
        add(f"**{LABEL[name]}** — лотов {len(open_lots)}, активные пары {', '.join(arm['active_symbols']) or 'нет'}")
        for lot in open_lots:
            add(f"- лот `{lot['lot_id']}`: {lot['remaining_base_qty']} {lot['base_asset']} @ {fmt(lot['entry_price'], '0.0001')}, стоимость {fmt(lot['quote_cost_remaining'])} {quote}, mark bid {fmt(lot.get('mark_bid'), '0.0001')}")
        for w in arm["working"]:
            if w["state"] in {"OPEN", "PARTIAL"}:
                add(f"- заявка `{w['client_order_id']}` {w['side']} {w['symbol']} @ {fmt(w['limit_price'], '0.0001')} ({w['state']})")
        add("")
    add("## Расстояние до ближайшей покупки")
    add("")
    add("Расстояние = (bid − лимит отдыхающей покупки) / bid × 100%. Ноль или отрицательное значение означало бы касание уровня. Минимум — самое близкое приближение за период по опросам монитора.")
    add("")
    add("| пара | копия | лимит покупки (финиш) | bid финиш | расстояние финиш % | мин. расстояние за период % | когда (UTC) | макс. % |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    distance_rows = 0
    for name in ARMS:
        symbols = sorted({sym for s in samples for sym in s.get("buy_distance", {}).get(name, {})})
        for symbol in symbols:
            series = [(s["at"], dec(s["buy_distance"][name][symbol]["pct"])) for s in samples if symbol in s.get("buy_distance", {}).get(name, {})]
            current = last.get("buy_distance", {}).get(name, {}).get(symbol)
            at_min, lo = min(series, key=lambda item: item[1])
            hi = max(p for _, p in series)
            add(
                f"| {symbol} | {LABEL[name]} | {fmt(current['limit'], '0.0001') if current else '— (снята/исполнена)'} | {fmt(current['bid'], '0.0001') if current else '—'} | "
                f"{fmt(current['pct'], '0.001') if current else '—'} | {fmt(lo, '0.001')} | {at_min[11:19]} | {fmt(hi, '0.001')} |"
            )
            distance_rows += 1
    if not distance_rows:
        add("| — | — | отдыхающих покупок не было | | | | | |")
    add("")
    add("## Рынок и лента")
    add("")
    add(f"| пара | bid старт | bid финиш | изменение % | мин. bid за {period_word} | макс. bid за {period_word} | опросов с ошибкой | опросов stale |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for symbol in first["feed"]:
        bids = [dec(s["feed"].get(symbol, {}).get("bid")) for s in samples if s["feed"].get(symbol, {}).get("bid") is not None]
        b0 = dec(first["feed"][symbol].get("bid"))
        b1 = dec(last["feed"].get(symbol, {}).get("bid"))
        change = ((b1 - b0) / b0 * 100) if b0 and b1 else None
        errs = sum(1 for s in samples if not s["feed"].get(symbol, {}).get("ok"))
        stale = sum(1 for s in samples if s["feed"].get(symbol, {}).get("stale"))
        add(f"| {symbol} | {fmt(b0, '0.0001')} | {fmt(b1, '0.0001')} | {fmt(change, '0.001')} | {fmt(min(bids), '0.0001') if bids else '—'} | {fmt(max(bids), '0.0001') if bids else '—'} | {errs} | {stale} |")
    add("")
    add("Шаг сетки 0,50% от якоря (best ask при первом появлении пары, после покупки — цена входа лота); для входа цене нужно опуститься на 0,50% ниже якоря, для выхода — подняться так, чтобы после комиссий 0,10%+0,10% и slippage 0,05% остаться в плюсе.")
    anchors = last.get("anchors", {}).get("baseline", {})
    if anchors:
        add("Якоря на конец периода (baseline): " + ", ".join(f"{s} {fmt(v, '0.0001')}" for s, v in sorted(anchors.items())) + f"; восстановлено заявок при последнем старте процесса: {last.get('restored_orders')}.")
    add("")
    add("## RiskManager и журнал решений")
    add("")
    for name in ARMS:
        arm = last["arms"][name]
        add(f"**{LABEL[name]}**: входов разрешено {arm['entries_allowed']}, отклонено {arm['entries_rejected']}; блокировка ledger: {arm['trading_blocked']}{(' (' + str(arm['block_reason']) + ')') if arm['block_reason'] else ''}.")
        if arm["reject_reasons"]:
            add("  причины отказов: " + ", ".join(f"`{k}` ×{v}" for k, v in sorted(arm["reject_reasons"].items(), key=lambda kv: -kv[1])))
        skips = {s: v for s, v in arm["strategy_skips"].items() if v}
        if skips:
            add("  пропуски стратегии: " + ", ".join(f"{s}: {','.join(v)}" for s, v in skips.items()))
    add("")
    first_ids = {(d["arm"], d["client_order_id"], d["reason"]) for d in first["decisions"]}
    new_decisions = [d for d in last["decisions"] if (d["arm"], d["client_order_id"], d["reason"]) not in first_ids]
    add(f"Новых строк в журнале решений за {period_word}: {len(new_decisions)}.")
    if new_decisions:
        add("")
        add("| время | копия | заявка | сторона | разрешено | причина | повторов |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for d in new_decisions[-40:]:
            add(f"| {d['at'][11:19]} | {d['arm']} | `{d['client_order_id']}` | {d['side']} | {d['allowed']} | {d['reason']} | {d.get('repeats', 1)} |")
    add("")
    controls_changed = any(s["controls"] != first["controls"] for s in samples)
    add(f"Ручные флаги (pause/halt/liquidate) за период: {'менялись' if controls_changed else 'не менялись'}; на конец: {last['controls']}.")
    regimes_seen = sorted({f"{k}:{v}" for s in samples for k, v in s["regimes"].items() if v != "NORMAL"})
    add(f"Режимы новостей, отличные от NORMAL, за период: {', '.join(regimes_seen) if regimes_seen else 'не было'} (реальный новостной ИИ не подключён — работает mock-источник без событий).")
    add("")
    add("## Ошибки, отключения, устаревшие котировки")
    add("")
    all_errors = {e["at"] + " " + e["error"] for s in samples for e in s["errors"]}
    add(f"Ошибок рантайма за период: {len(all_errors)}.")
    for item in sorted(all_errors)[:30]:
        add(f"- {item}")
    add(f"Недоступность API монитору (пропущенные опросы): {len(outages)}.")
    for item in outages[:30]:
        add(f"- {item}")
    tick_gaps = []
    for prev, cur in zip(samples, samples[1:]):
        if cur["ticks"] == prev["ticks"]:
            tick_gaps.append(cur["at"])
    add(f"Опросов без нового тика (интервал опроса монитора 60 с, тик каждые ~5–7 с): {len(tick_gaps)}.")
    for item in tick_gaps[:20]:
        add(f"- {item}")
    add("")
    add("## Траектория equity (по опросам монитора)")
    add("")
    add("| время UTC | тик | baseline equity | AI equity | baseline free/reserved | AI free/reserved | лоты b/ai | циклы b/ai |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    step = max(1, len(samples) // 20)
    for s in samples[::step] + ([samples[-1]] if (len(samples) - 1) % step else []):
        b, a = s["arms"]["baseline"], s["arms"]["ai"]
        add(f"| {s['at'][11:19]} | {s['ticks']} | {fmt(b['equity'])} | {fmt(a['equity'])} | {fmt(b['free'])}/{fmt(b['reserved'])} | {fmt(a['free'])}/{fmt(a['reserved'])} | {b['open_lots']}/{a['open_lots']} | {b['completed_cycles']}/{a['completed_cycles']} |")
    add("")
    add("## Выводы")
    add("")
    total_new_fills = sum(len([f for f in last["arms"][n]["fills"] if f["fill_id"] not in set(first["arms"][n]["fill_ids"])]) for n in ARMS)
    live_ok = all(s["health"].get("live_trading") is False and s["health"].get("live_adapter") == "disabled" for s in samples)
    add(f"- Live-торговля: {'отключена на всех опросах' if live_ok else 'ВНИМАНИЕ: /health показал отличное от disabled значение'}.")
    add(f"- Непрерывность: тиков с {first['ticks']} до {last['ticks']} за {int((ended - started).total_seconds() // 60)} мин; ошибок рантайма {len(all_errors)}; пропусков опроса {len(outages)}.")
    if total_new_fills == 0:
        add("- Сделок не было. По журналу решений и ленте: ")
        for symbol in first["feed"]:
            bids = [dec(s["feed"].get(symbol, {}).get("bid")) for s in samples if s["feed"].get(symbol, {}).get("bid") is not None]
            if not bids:
                continue
            b0 = bids[0]
            drop = (min(bids) - b0) / b0 * 100
            series = [dec(s["buy_distance"]["baseline"][symbol]["pct"]) for s in samples if symbol in s.get("buy_distance", {}).get("baseline", {})]
            nearest = f", ближе всего к уровню покупки подходила на {fmt(min(series), '0.001')}%" if series else " (отдыхающей покупки по паре не было)"
            add(f"  - {symbol}: минимальное отклонение bid от начала периода {fmt(drop, '0.001')}%{nearest}.")
        add("  - Ограничения RiskManager, если были, перечислены выше в причинах отказов. Настройки ради увеличения числа сделок не менялись.")
    else:
        add(f"- Исполнено fills за {period_word}: {total_new_fills}. Комиссии учтены в каждой сделке; P&L выше — после комиссий.")
    identical = first["arms"]["baseline"]["equity"] == first["arms"]["ai"]["equity"] and last["arms"]["baseline"]["equity"] == last["arms"]["ai"]["equity"]
    add(f"- A/B: {'обе копии шли одинаково — без новостных событий AI-фильтр не отличается от базовой сетки, что ожидаемо' if identical else 'копии разошлись; причина — вето AI или различия в исполнении, см. журнал решений'}.")
    add("- Этот период проверяет торговую механику (лента, резервы, матчинг, учёт комиссий, персистентность), а не качество новостного анализа: реальный ИИ и источники новостей не подключены.")
    add("")
    add(f"Сырые опросы: `{samples_file}`, лог монитора: `{log_file}`, лог процесса: `var/serve.log`.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only observer for the paper runtime (1 h by default; 24 h with hourly checkpoints via --checkpoint-dir).")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--out", default="reports/PAPER_1H_REPORT.md")
    parser.add_argument("--samples", default="var/watch_1h.jsonl")
    parser.add_argument("--log-label", default=None, help="monitor log path to mention in the report (default: samples path with .log)")
    parser.add_argument("--config-label", default="configs/paper.binance_public.usdt.yaml")
    parser.add_argument("--title", default=None, help="report title (default: PAPER 1H REPORT / PAPER 24H REPORT by --minutes)")
    parser.add_argument("--checkpoint-dir", default=None, help="write hour_NN.md here every --checkpoint-minutes (each covers that hour only)")
    parser.add_argument("--checkpoint-minutes", type=int, default=60)
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    deadline = started + timedelta(minutes=args.minutes)
    hours = args.minutes / 60
    title = args.title or (f"PAPER {int(hours)}H REPORT" if hours == int(hours) else f"PAPER {args.minutes}MIN REPORT")
    period_word = "час" if args.minutes <= 60 else ("сутки" if args.minutes == 1440 else "период")
    samples_path = Path(args.samples)
    samples_path.parent.mkdir(parents=True, exist_ok=True)
    log_label = args.log_label or str(samples_path.with_suffix(".log"))
    checkpoint_dir = Path(args.checkpoint_dir) if args.checkpoint_dir else None
    samples: list[dict] = []
    outages: list[str] = []
    first: dict | None = None
    last: dict | None = None
    hour_first: dict | None = None
    hour_started = started
    hour_index = 1
    next_checkpoint = started + timedelta(minutes=args.checkpoint_minutes)
    print(f"[watch] start {started.isoformat()} until {deadline.isoformat()} -> {args.out}" + (f" (checkpoints in {checkpoint_dir})" if checkpoint_dir else ""), flush=True)
    while True:
        now = datetime.now(timezone.utc)
        try:
            sample = snapshot(args.base_url)
            if first is None:
                first = sample
                print(f"[watch] initial: ticks={sample['ticks']} " + " ".join(f"{n}:eq={sample['arms'][n]['equity']} free={sample['arms'][n]['free']} res={sample['arms'][n]['reserved']} lots={sample['arms'][n]['open_lots']} cycles={sample['arms'][n]['completed_cycles']}" for n in ARMS), flush=True)
            if hour_first is None:
                hour_first = sample
            last = sample
            samples.append(sample)
            with samples_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(light(sample), ensure_ascii=False) + "\n")
            b, a = sample["arms"]["baseline"], sample["arms"]["ai"]
            near = " ".join(f"{s.split('/')[0]}:{d['pct']}%" for s, d in sorted(sample["buy_distance"].get("baseline", {}).items()))
            print(f"[watch] {now.isoformat(timespec='seconds')} ticks={sample['ticks']} b:eq={b['equity']} fills={b['buys']}/{b['sells']} cyc={b['completed_cycles']} ai:eq={a['equity']} fills={a['buys']}/{a['sells']} cyc={a['completed_cycles']} errors={len(sample['errors'])} dist[{near}] paused={sample['paused_reasons'][:3]}", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep observing
            outages.append(f"{now.isoformat(timespec='seconds')} {type(exc).__name__}: {exc}")
            print(f"[watch] {now.isoformat(timespec='seconds')} poll failed: {type(exc).__name__}: {exc}", flush=True)
        if checkpoint_dir is not None and now >= next_checkpoint and now < deadline:
            if hour_first is not None and last is not None:
                hour_samples = [s for s in samples if s["at"] >= hour_first["at"]]
                hour_outages = [o for o in outages if o[:19] >= hour_started.isoformat(timespec="seconds")[:19]]
                target = checkpoint_dir / f"hour_{hour_index:02d}.md"
                try:
                    write_report(
                        target, hour_first, last, hour_samples, args.config_label, hour_started, now, hour_outages,
                        title=f"{title} — контрольная точка, час {hour_index}", period_word="час", samples_file=str(samples_path), log_file=log_label,
                    )
                    print(f"[watch] checkpoint written: {target}", flush=True)
                except Exception as exc:  # noqa: BLE001 - a report bug must not stop the observation
                    print(f"[watch] checkpoint failed: {type(exc).__name__}: {exc}", flush=True)
            hour_index += 1
            hour_started = now
            hour_first = None
            next_checkpoint = next_checkpoint + timedelta(minutes=args.checkpoint_minutes)
        if now >= deadline:
            break
        time.sleep(min(args.interval, max(1, int((deadline - now).total_seconds()))))
    ended = datetime.now(timezone.utc)
    if first is None or last is None:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(f"# {title}\n\nМонитор не получил ни одного ответа от API за период.\n\n" + "\n".join(outages), encoding="utf-8")
        print("[watch] no samples; wrote failure report", flush=True)
        return 1
    write_report(Path(args.out), first, last, samples, args.config_label, started, ended, outages, title=title, period_word=period_word, samples_file=str(samples_path), log_file=log_label)
    print(f"[watch] report written: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
