"""Separate Telegram process: `python -m app.telegram [--config PATH] [--test-message]`.

It never builds a PaperRuntime, never opens the trading databases and never imports an exchange
adapter. Everything it knows comes from GET /api/state on loopback or the private paper host.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import load_settings
from app.telegram.bot import PollingLock, build_remote_app, lock_path_for
from app.telegram.client import StateUnavailable

log = logging.getLogger("ats.telegram")


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # aiogram logs the request URL (which contains the token) at DEBUG; keep it at INFO or above.
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.telegram", description="Read-only Telegram bot for the local paper runtime.")
    parser.add_argument("--config", default=None, help="YAML config (same as the serve process)")
    parser.add_argument("--test-message", action="store_true", help="send one test notification and exit")
    args = parser.parse_args(argv)
    _configure_logging()

    settings = load_settings(args.config)
    if not settings.telegram_enabled:
        print("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS in .env", file=sys.stderr)
        return 2
    if settings.telegram_mode != "process":
        print("ATS_TELEGRAM_MODE is 'inprocess': the API process owns polling; refusing to start a second poller", file=sys.stderr)
        return 2

    app = build_remote_app(settings)
    try:
        health = app.client.health()  # type: ignore[attr-defined]
    except StateUnavailable as exc:
        log.warning("paper API not reachable yet (%s); the bot will keep trying", exc)
    else:
        log.info("paper API ok: mode=%s live_trading=%s ticks=%s", health.get("mode"), health.get("live_trading"), health.get("ticks"))

    if args.test_message:
        asyncio.run(_test(app))
        return 0

    lock = PollingLock(lock_path_for(settings))
    if not lock.acquire():
        print(f"another Telegram process already polls this token (lock {lock.path}); exiting", file=sys.stderr)
        return 3
    try:
        asyncio.run(_run_forever(app))
    except KeyboardInterrupt:
        pass
    finally:
        lock.unlock()
    return 0


async def _test(app) -> None:  # type: ignore[no-untyped-def]
    try:
        await app.send_test()
        log.info("test notification sent to %d target(s)", len(app.settings.telegram_user_ids) or 1)
    finally:
        await app.bot.session.close()


async def _run_forever(app) -> None:  # type: ignore[no-untyped-def]
    while True:
        try:
            await app.run()
            return
        except Exception:  # noqa: BLE001
            log.exception("telegram polling failed; retrying in 30 s")
            await asyncio.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
