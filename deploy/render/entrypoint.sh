#!/bin/sh
# Render start: paper (private API + tick) or telegram (read-only worker).
# Disk is mounted at /app/var. LiveAdapter stays disabled.
set -eu
cd /app
mkdir -p /app/var

role="${1:-${ATS_ROLE:-paper}}"

if [ -n "${ATS_TELEGRAM_API_BASE_URL:-}" ]; then
  case "$ATS_TELEGRAM_API_BASE_URL" in
    http://*|https://*) ;;
    *) export ATS_TELEGRAM_API_BASE_URL="http://$ATS_TELEGRAM_API_BASE_URL" ;;
  esac
fi

if [ "$role" = "paper" ]; then
  if [ "${ATS_REQUIRE_EXISTING_DB:-false}" = "true" ]; then
    while [ ! -f /app/var/local.db ]; do
      echo "ats-paper: waiting for /app/var/local.db — restore the Mac backup (docs/RENDER.md)" >&2
      sleep 5
    done
  fi
  export ATS_CONFIG_PATH="${ATS_CONFIG_PATH:-configs/paper.binance_public.usdt.yaml}"
  export ATS_RUN_LOOP="${ATS_RUN_LOOP:-true}"
  port="${PORT:-10000}"
  exec python -m app.cli serve --config "$ATS_CONFIG_PATH" --host 0.0.0.0 --port "$port"
fi

if [ "$role" = "telegram" ]; then
  export ATS_CONFIG_PATH="${ATS_CONFIG_PATH:-configs/paper.binance_public.usdt.yaml}"
  exec python -m app.telegram --config "$ATS_CONFIG_PATH"
fi

exec "$@"
