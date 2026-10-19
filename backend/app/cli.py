"""Local commands. `run` advances the synthetic paper loop offline; `serve` starts the API."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import load_settings
from app.paper.bootstrap import build_runtime
from app.paper.runtime import to_jsonable


def _settings(path: str | None):  # type: ignore[no-untyped-def]
    candidate = Path(path) if path else Path("configs/paper.example.yaml")
    if not candidate.is_file():
        raise SystemExit(f"config not found: {candidate}")
    return load_settings(candidate)


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args.config)
    runtime = build_runtime(settings, persist=not args.no_persist)
    now = datetime.now(timezone.utc)
    for index in range(args.steps):
        report = runtime.tick(now + timedelta(seconds=index * settings.market_poll_interval_sec))
        if args.verbose:
            print(json.dumps(to_jsonable(report), separators=(",", ":")))
    snapshot = runtime.snapshot()
    summary = {
        "ticks": snapshot["runtime"]["tick_count"],
        "feed": snapshot["feed"]["name"],
        "baseline_equity": snapshot["arms"]["baseline"].get("equity"),
        "ai_equity": snapshot["arms"]["ai"].get("equity"),
        "equity_delta": snapshot["ab"]["equity_delta"],
        "baseline_cycles": snapshot["arms"]["baseline"]["completed_cycles"],
        "ai_cycles": snapshot["arms"]["ai"]["completed_cycles"],
        "paused_reasons": snapshot["paused_reasons"],
        "errors": snapshot["runtime"]["errors"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    from app.ops.lock import InstanceLock, instance_lock_path

    os.environ.setdefault("ATS_RUN_LOOP", "true")
    if args.config:
        os.environ["ATS_CONFIG_PATH"] = args.config
    settings = _settings(args.config)
    lock = InstanceLock(Path(args.lock) if args.lock else instance_lock_path(settings.database_url))
    if not lock.acquire():
        raise SystemExit(f"another paper process holds {lock.path}; refusing a second engine")
    try:
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)
    finally:
        lock.unlock()
    return 0


def cmd_market_sync(args: argparse.Namespace) -> int:
    from datetime import date, timedelta

    from app.market.archive import sync_range
    from app.market.history import CandleStore

    store = CandleStore(Path(args.dest))
    today = datetime.now(timezone.utc).date()
    report = []
    for base in [item.strip().upper() for item in args.symbols.split(",") if item.strip()]:
        symbol = f"{base}/{args.quote.upper()}"
        for interval, days in (("1m", args.days_1m), ("5m", args.days_5m), ("1h", args.days_1h)):
            start = today - timedelta(days=days)
            row = sync_range(store, symbol, interval, start, today)
            report.append(row)
            print(json.dumps(row, ensure_ascii=False, default=str))
    store.close()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from app.market.history import CandleStore
    from app.research.backtest import compare_steps

    settings = _settings(args.config)
    store = CandleStore(Path(args.candles))
    candles = store.load(args.symbol, args.interval)
    if len(candles) < 20:
        raise SystemExit(f"not enough candles in {args.candles} for {args.symbol} {args.interval}: {len(candles)}")
    result = compare_steps(settings, store, args.symbol, args.interval, start=candles[0].open_time, end=candles[-1].open_time)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# BACKTEST_REPORT",
        "",
        f"Символ {result.get('symbol')} интервал {result.get('interval')} баров {result.get('bars')}.",
        "Live PaperRuntime и его БД не использовались.",
        f"Режим (эвристика, не прогноз): {result.get('regime')}",
        f"ATR%: {result.get('atr_pct')} адаптивный шаг: {result.get('adaptive_step_pct')}",
        "",
        "| period | step | adaptive | equity | realized | cycles | buys | sells |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in result.get("runs") or []:
        lines.append(
            f"| {row['period']} | {row['step_pct']} | {row['adaptive']} | {row['equity']} | {row['realized_pnl']} | {row['completed_cycles']} | {row['buys']} | {row['sells']} |"
        )
    lines += ["", result.get("disclaimer") or "", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    store.close()
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from app.ops.backup import backup_sqlite, verify_backup

    settings = _settings(args.config)
    folder = backup_sqlite(settings.database_url, Path(args.dest))
    report = verify_backup(folder)
    print(json.dumps({"folder": str(folder), "verify": report}, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="ats", description="Paper-only runtime. No live orders.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="advance the paper loop offline")
    run.add_argument("--config", default=None)
    run.add_argument("--steps", type=int, default=120)
    run.add_argument("--no-persist", action="store_true")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(func=cmd_run)
    serve = sub.add_parser("serve", help="start API plus background loop")
    serve.add_argument("--config", default=None)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--lock", default=None, help="override the per-database instance lock path")
    serve.set_defaults(func=cmd_serve)
    backup = sub.add_parser("backup", help="copy sqlite files and verify the snapshot against replay")
    backup.add_argument("--config", default=None)
    backup.add_argument("--dest", default="var/backups")
    backup.set_defaults(func=cmd_backup)
    market = sub.add_parser("market-sync", help="download Binance Vision klines into a local candle store")
    market.add_argument("--dest", default="var/market/candles.db")
    market.add_argument("--symbols", default="BTC,ETH,SOL,XRP")
    market.add_argument("--quote", default="USDT")
    market.add_argument("--days-1m", type=int, default=90)
    market.add_argument("--days-5m", type=int, default=180)
    market.add_argument("--days-1h", type=int, default=365)
    market.set_defaults(func=cmd_market_sync)
    replay = sub.add_parser("replay", help="offline candle replay; never opens the live paper DB")
    replay.add_argument("--config", default="configs/paper.binance_public.usdt.yaml")
    replay.add_argument("--candles", default="var/market/candles.db")
    replay.add_argument("--symbol", default="BTC/USDT")
    replay.add_argument("--interval", default="1h")
    replay.add_argument("--out", default="reports/BACKTEST_REPORT.md")
    replay.set_defaults(func=cmd_replay)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
