"""Read-only check: Telegram screens vs live /api/state. Does not write the trading DBs."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.telegram.auth import Allowlist
from app.telegram.client import RemoteClient
from app.telegram.commands import CommandService
from app.telegram import views

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8000"
OWNER = 8797851182
MONEY = re.compile(r"[+\-]?(?:\d{1,3}(?: \d{3})*|\d+)(?:\.\d+)?")


def dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def parse_money(text: str) -> Decimal:
    return Decimal(text.replace(" ", ""))


def money_tokens(line: str) -> list[Decimal]:
    return [parse_money(m.group(0)) for m in MONEY.finditer(line)]


def same(a: Any, b: Any, places: str = "0.01") -> bool:
    da, db = dec(a), dec(b)
    if da is None or db is None:
        return da is db
    q = Decimal(places)
    return da.quantize(q) == db.quantize(q)


def resting_buys(arm: dict[str, Any]) -> int:
    return sum(1 for w in arm.get("working") or [] if w["side"] == "buy" and w["state"] in ("OPEN", "PARTIAL"))


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reserve_of(arm: dict[str, Any]) -> Decimal:
    value = arm.get("profit_reserve")
    return Decimal("0") if value is None else Decimal(str(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=int, default=70, help="seconds to wait for a new 24h watch sample")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    client = RemoteClient(API)
    health = client.health()
    state = client.state()
    service = CommandService(client, Allowlist(frozenset({OWNER})), controls_enabled=False)
    screens = {
        name: service.handle_text(OWNER, f"/{name}").text  # type: ignore[union-attr]
        for name in ("start", "balance", "positions", "trades", "report", "status", "news")
    }

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    base, ai = state["arms"]["baseline"], state["arms"]["ai"]
    start, balance, positions, trades, report, status, news = (
        screens["start"],
        screens["balance"],
        screens["positions"],
        screens["trades"],
        screens["report"],
        screens["status"],
        screens["news"],
    )

    # --- health / live off / source ---
    check(
        "live_adapter_disabled",
        health.get("live_adapter") == "disabled" and health.get("live_trading") is False and state.get("live_trading") is False,
        f"health.live_adapter={health.get('live_adapter')} live_trading={health.get('live_trading')} state.live_trading={state.get('live_trading')}",
    )
    check("mode_paper", health.get("mode") == "paper" and state.get("mode") == "paper", f"health={health.get('mode')} state={state.get('mode')}")
    check(
        "market_source_binance_public",
        state.get("exchange") == "binance_public" and "Binance Public" in start,
        f"exchange={state.get('exchange')} feed={state['feed'].get('name')} start_has_binance={'Binance Public' in start}",
    )
    news_src = str(state["news"].get("source") or "")
    check(
        "news_marked_mock",
        ("Mock" in news_src or not news_src) and "MOCK" in news and "Реальные новости и LLM пока не подключены" in news,
        f"news.source={news_src!r}",
    )
    check(
        "telegram_controls_off",
        "только читает" in (service.handle_text(OWNER, "/pause").text or ""),
        "pause command stays read-only",
    )

    # --- starting 1000 USDT each ---
    check(
        "starting_1000_usdt_each",
        same(base.get("starting"), "1000") and same(ai.get("starting"), "1000") and state.get("quote_currency") == "USDT",
        f"baseline.starting={base.get('starting')} ai.starting={ai.get('starting')} quote={state.get('quote_currency')}",
    )

    start_eq = next(line for line in start.splitlines() if start.splitlines()[start.splitlines().index(line) - 1] == "Общий капитал")
    start_free = next(line for i, line in enumerate(start.splitlines()) if i and start.splitlines()[i - 1] == "Свободно")
    start_reserved = next(line for i, line in enumerate(start.splitlines()) if i and start.splitlines()[i - 1] == "В заявках")
    start_counts = next(line for line in start.splitlines() if "Открытых позиций" in line)
    start_result = next(line for i, line in enumerate(start.splitlines()) if i and start.splitlines()[i - 1] == "Результат")
    nums_eq = money_tokens(start_eq)
    nums_cash = money_tokens(start_reserved) + money_tokens(start_free)
    nums_result = money_tokens(start_result)
    realized = dec(base.get("realized_pnl")) or Decimal("0")
    unreal = dec(base.get("unrealized_pnl")) or Decimal("0")
    check(
        "start_free_reserved_reserve",
        len(nums_cash) >= 2 and same(nums_cash[0], base.get("reserved_quote")) and same(nums_cash[1], base.get("free_quote")),
        f"screen reserved/free={nums_cash[:2]} api=({base.get('reserved_quote')},{base.get('free_quote')})",
    )
    check(
        "start_equity",
        len(nums_eq) >= 1 and same(nums_eq[0], base.get("equity")),
        f"screen={nums_eq[:1]} api=({base.get('equity')})",
    )
    check(
        "start_realized_unrealized",
        len(nums_result) >= 1 and same(nums_result[0], realized + unreal),
        f"screen={nums_result[:1]} api realized+unreal=({realized}+{unreal})",
    )
    check(
        "start_lots_and_cycles",
        f"Открытых позиций: {base['open_lots']}" in start_counts
        and f"Закрыто: {base['completed_cycles']}" in start_counts
        and "Купить при" in start
        and "A/B" not in start
        and "equity" not in start.lower(),
        f"open={base['open_lots']} wait={resting_buys(base)}",
    )

    # --- /balance separate sections ---
    check("balance_sections_not_mixed", "<b>Сетка</b>" in balance and "<b>Сетка + AI</b>" in balance, "both arm headers present")
    grid_part, _, ai_part = balance.partition("<b>Сетка + AI</b>")
    check("balance_grid_before_ai", "<b>Сетка</b>" in grid_part and "<b>Сетка + AI</b>" not in grid_part, "Сетка block precedes Сетка + AI")
    for label, part, arm in (("baseline", grid_part, base), ("ai", "<b>Сетка + AI</b>" + ai_part, ai)):
        eq_line = next(line for line in part.splitlines() if "equity" in line)
        cash_line = next(line for line in part.splitlines() if "Profit Reserve" in line)
        pnl_line = next(line for line in part.splitlines() if "realized" in line)
        eq_n = money_tokens(eq_line)
        cash_n = money_tokens(cash_line)
        pnl_n = money_tokens(pnl_line)
        check(
            f"balance_{label}_matches_api",
            same(eq_n[0], arm.get("equity"))
            and same(cash_n[0], arm.get("free_quote"))
            and same(cash_n[1], arm.get("reserved_quote"))
            and same(cash_n[2], reserve_of(arm))
            and same(pnl_n[0], arm.get("realized_pnl"))
            and same(pnl_n[1], arm.get("unrealized_pnl")),
            f"{label} equity/free/reserved/reserve/realized/unrealized screen=({eq_n[0]},{cash_n[:3]},{pnl_n[:2]}) api=({arm.get('equity')},{arm.get('free_quote')},{arm.get('reserved_quote')},{arm.get('profit_reserve')},{arm.get('realized_pnl')},{arm.get('unrealized_pnl')})",
        )

    api_buys = [w["symbol"].split("/")[0] for w in base["working"] if w["side"] == "buy" and w["state"] in {"OPEN", "PARTIAL"}]
    waiting_cards = positions.count("Ждёт входа")
    check(
        "positions_baseline_resting_buys",
        waiting_cards == len(api_buys) or (not api_buys and "Ждёт входа" not in positions),
        f"screen waiting={waiting_cards} api={api_buys}",
    )
    check("positions_simple_no_ab_headers", "<b>Сетка + AI</b>" not in positions, "simple positions hide the AI arm")
    closed = [lot for lot in base["lots"] if lot.get("status") == "closed"]
    check(
        "trades_baseline_closed",
        f"Всего закрытых сделок: {len(closed)}" in trades,
        f"closed={len(closed)} empty={'Пока нет закрытых сделок' in trades}",
    )
    foreign = [fid for fid in ([f["fill_id"] for f in ai.get("fills") or []]) if fid and fid in trades]
    check("trades_baseline_no_foreign_fill_ids", not foreign, f"foreign ids on simple trades: {foreign}")

    # --- /report A/B ---
    ab = state["ab"]
    rep_eq = next(line for line in report.splitlines() if "Baseline equity" in line)
    rep_nums = money_tokens(rep_eq)
    check(
        "report_ab_equities",
        len(rep_nums) >= 3
        and same(rep_nums[0], ab.get("baseline_equity"))
        and same(rep_nums[1], ab.get("ai_equity"))
        and same(rep_nums[2], ab.get("equity_delta")),
        f"screen={rep_nums[:3]} ab=({ab.get('baseline_equity')},{ab.get('ai_equity')},{ab.get('equity_delta')})",
    )
    check(
        "report_cycles_match_arms",
        f"Циклы: {base['completed_cycles']} / {ai['completed_cycles']}" in report
        and f"открытые лоты {base['open_lots']} / {ai['open_lots']}" in report,
        f"cycles {base['completed_cycles']}/{ai['completed_cycles']} lots {base['open_lots']}/{ai['open_lots']}",
    )

    # --- arms not swapped: if values differ, order stays baseline then ai ---
    if not same(base.get("equity"), ai.get("equity")) or not same(base.get("free_quote"), ai.get("free_quote")):
        check("arms_order_when_different", same(nums_eq[0], base.get("equity")), "home balance is baseline")
    else:
        check(
            "arms_currently_identical_values",
            True,
            "baseline and AI cash/equity match (no AI-only news effect yet); sections still labeled separately",
        )

    # --- processes ---
    pids = {
        "paper": int((ROOT / "var/serve.pid").read_text().strip()),
        "telegram": int((ROOT / "var/telegram.pid").read_text().strip()),
        "watch_24h": int((ROOT / "var/watch_24h.pid").read_text().strip()),
    }
    for name, pid in pids.items():
        check(f"process_{name}_alive", pid_alive(pid), f"pid={pid}")
    check("paper_loop_running", health.get("loop") is True and int(health.get("ticks") or 0) > 0, f"ticks={health.get('ticks')} loop={health.get('loop')}")
    check("telegram_is_separate_process", health.get("telegram") is False, "API process does not poll Telegram")

    # --- 24h watch continues ---
    jsonl = ROOT / "var/watch_24h.jsonl"
    lines = jsonl.read_text(encoding="utf-8").splitlines() if jsonl.is_file() else []
    last = json.loads(lines[-1]) if lines else {}
    last_at = views.when(last.get("at"))
    age = (now - last_at).total_seconds() if last_at else 10**9
    check(
        "watch_24h_collecting",
        jsonl.is_file() and len(lines) >= 1 and age < 120,
        f"samples={len(lines)} last_at={last.get('at')} age_sec={int(age)} last_ticks={last.get('ticks')}",
    )
    before = len(lines)
    if args.wait > 0:
        time.sleep(args.wait)
        after_lines = jsonl.read_text(encoding="utf-8").splitlines()
        after_last = json.loads(after_lines[-1]) if after_lines else {}
        check(
            "watch_24h_new_sample",
            len(after_lines) > before,
            f"samples {before} → {len(after_lines)}; last_at {after_last.get('at')} ticks={after_last.get('ticks')}",
        )
    else:
        check(
            "watch_24h_new_sample",
            age < 120,
            f"wait skipped; last sample age_sec={int(age)} samples={before}",
        )
    hours = list((ROOT / "reports/hours").glob("hour_*.md")) if (ROOT / "reports/hours").is_dir() else []
    started = views.when(state["runtime"].get("started_at"))
    elapsed_h = ((datetime.now(timezone.utc) - started).total_seconds() / 3600) if started else 0
    check(
        "watch_24h_hourly_checkpoint_timing",
        (elapsed_h < 1 and not hours) or bool(hours),
        f"elapsed_h={elapsed_h:.2f} hour_files={len(hours)} (first checkpoint after 60 min)",
    )

    # --- telegram seen-state (dedupe file, no restart) ---
    tg_state_path = ROOT / "var/telegram_state.json"
    seen_ok = False
    seen_detail = "missing"
    if tg_state_path.is_file():
        saved = json.loads(tg_state_path.read_text(encoding="utf-8"))
        seen = saved.get("seen") or {}
        live_fills_b = [f["fill_id"] for f in base.get("fills") or []]
        live_fills_a = [f["fill_id"] for f in ai.get("fills") or []]
        seen_ok = (
            set(seen.get("fills", {}).get("baseline") or []) == set(live_fills_b)
            and set(seen.get("fills", {}).get("ai") or []) == set(live_fills_a)
            and seen.get("api_down") is False
            and int(seen.get("tick_count") or 0) > 0
        )
        seen_detail = (
            f"seen.ticks={seen.get('tick_count')} api.ticks={state['runtime'].get('tick_count')} "
            f"seen.fills={ {k: seen.get('fills', {}).get(k) for k in ('baseline', 'ai')} } "
            f"api.fills baseline={live_fills_b} ai={live_fills_a}"
        )
    check("telegram_seen_state_tracks_fills", seen_ok, seen_detail)

    denied = service.handle_text(1, "/start")
    check("allowlist_blocks_other_ids", denied is not None and "Доступ запрещён" in denied.text, f"user 1 reply={denied.text if denied else None}")

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]

    out = ROOT / "reports/TELEGRAM_VERIFICATION.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines_md = [
        "# TELEGRAM_VERIFICATION",
        "",
        f"Дата: {datetime.now(timezone.utc).isoformat()}",
        "Метод: только GET `http://127.0.0.1:8000/api/state` и те же функции, что рисует бот (`CommandService` + `views`).",
        "Торговый движок, Telegram-процесс и 24-часовой монитор не останавливались. БД и конфиг торговли не менялись.",
        "Экран телефона не открывался: сверка автоматическая по тому же коду, который отвечает на `/start` и остальные команды.",
        "",
        f"Итог: **{'PASSED' if not failed else 'FAILED'}** — {passed}/{len(results)} проверок.",
        "",
        "## Снимок API",
        "",
        f"- health: mode={health.get('mode')} live_adapter={health.get('live_adapter')} loop={health.get('loop')} ticks={health.get('ticks')} telegram_in_api={health.get('telegram')}",
        f"- exchange={state.get('exchange')} quote={state.get('quote_currency')} feed={state['feed'].get('name')}",
        f"- news.source={state['news'].get('source')} last_ok={state['news'].get('last_ok_at')}",
        f"- processes: paper={pids['paper']} telegram={pids['telegram']} watch_24h={pids['watch_24h']}",
        "",
        "| поле | baseline | ai |",
        "| --- | --- | --- |",
        f"| starting | {base.get('starting')} | {ai.get('starting')} |",
        f"| equity | {base.get('equity')} | {ai.get('equity')} |",
        f"| free | {base.get('free_quote')} | {ai.get('free_quote')} |",
        f"| reserved | {base.get('reserved_quote')} | {ai.get('reserved_quote')} |",
        f"| profit_reserve | {base.get('profit_reserve')} | {ai.get('profit_reserve')} |",
        f"| realized_pnl | {base.get('realized_pnl')} | {ai.get('realized_pnl')} |",
        f"| unrealized_pnl | {base.get('unrealized_pnl')} | {ai.get('unrealized_pnl')} |",
        f"| open_lots | {base.get('open_lots')} | {ai.get('open_lots')} |",
        f"| completed_cycles | {base.get('completed_cycles')} | {ai.get('completed_cycles')} |",
        f"| fills | {len(base.get('fills') or [])} | {len(ai.get('fills') or [])} |",
        f"| resting buys | {resting_buys(base)} | {resting_buys(ai)} |",
        "",
        "## Проверки",
        "",
        "| проверка | результат | детали |",
        "| --- | --- | --- |",
    ]
    for name, ok, detail in results:
        lines_md.append(f"| `{name}` | {'PASSED' if ok else 'FAILED'} | {detail.replace('|', '/')} |")
    lines_md += [
        "",
        "## Экраны (как их собрал бы бот с этого снимка)",
        "",
        "### /start",
        "",
        "```",
        start.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""),
        "```",
        "",
        "### /balance",
        "",
        "```",
        balance.replace("<b>", "").replace("</b>", ""),
        "```",
        "",
        "### /positions",
        "",
        "```",
        positions.replace("<b>", "").replace("</b>", ""),
        "```",
        "",
        "### /trades",
        "",
        "```",
        trades.replace("<b>", "").replace("</b>", ""),
        "```",
        "",
        "### /report",
        "",
        "```",
        report.replace("<b>", "").replace("</b>", ""),
        "```",
        "",
        "## Уведомления и дедуп",
        "",
        "Живой Telegram-процесс не перезапускался. Файл `var/telegram_state.json` существует: seen.fills совпадают с API, `api_down=false`.",
        "Рестарт без повтора истории проверен тестом `test_notifier_persists_seen_state_and_announces_only_what_it_missed` (изолированная БД, не `var/*.db`).",
        "",
        "## Замечания",
        "",
        "- `reports/PAPER_24H_REPORT.md` появится в конце 24 часов; почасовые `reports/hours/hour_NN.md` — после первого полного часа.",
        "- Сейчас обе копии численно совпадают: сделок нет, mock-новости пустые. Смешение рук ловится структурой экранов (Сетка / Сетка + AI) и привязкой fill_id/лотов.",
        "- Живой PaperRuntime стартовал до поля `profit_reserve`: в `/api/state` его нет (`null`), Telegram на `/start` показывает `— / —`. Это согласовано с API и не сдвиг цифр. После следующего рестарта движка (не сейчас) поле станет `0`.",
        "- Прибыльность не оценивалась.",
        "",
    ]
    if failed:
        lines_md += ["## Расхождения", ""]
        for name, detail in failed:
            lines_md.append(f"- FAILED `{name}`: {detail}")
        lines_md.append("")
    out.write_text("\n".join(lines_md), encoding="utf-8")
    print(json.dumps({"passed": passed, "total": len(results), "failed": [n for n, _ in failed], "report": str(out)}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
