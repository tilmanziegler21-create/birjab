"""Local health probe for systemd. Talks only to 127.0.0.1 / localhost."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse


def _require_loopback(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(f"healthcheck refuses non-loopback URL: {url}")


def check(url: str, *, max_lag_sec: int) -> dict[str, object]:
    _require_loopback(url)
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback only
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("live_adapter") != "disabled" or payload.get("live_trading") is not False:
        raise SystemExit("healthcheck: live trading is not disabled")
    if payload.get("mode") != "paper":
        raise SystemExit("healthcheck: mode is not paper")
    last = payload.get("last_tick_at")
    if last and max_lag_sec > 0:
        moment = datetime.fromisoformat(str(last))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        lag = (datetime.now(timezone.utc) - moment).total_seconds()
        if lag > max_lag_sec:
            raise SystemExit(f"healthcheck: last tick is {int(lag)}s old")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ops.health")
    parser.add_argument("--url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--max-lag", type=int, default=60, help="0 disables the tick-age check")
    args = parser.parse_args(argv)
    try:
        payload = check(args.url, max_lag_sec=args.max_lag)
    except urllib.error.URLError as exc:
        print(f"healthcheck: unreachable ({exc})", file=sys.stderr)
        return 1
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "ticks": payload.get("ticks"), "last_tick_at": payload.get("last_tick_at")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
