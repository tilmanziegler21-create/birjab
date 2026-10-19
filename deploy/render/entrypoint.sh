#!/bin/sh
# Render start: paper (private API + tick) or telegram (read-only worker).
# Disk is mounted at /app/var. LiveAdapter stays disabled.
set -eu
cd /app
mkdir -p /app/var

# Dashboard env is easy to miss. A file on the persistent disk also works.
for f in /app/var/telegram.env /app/var/.env; do
  if [ -f "$f" ]; then
    echo "ats: loading $f" >&2
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
  fi
done

role="${1:-${ATS_ROLE:-paper}}"

if [ -n "${ATS_TELEGRAM_API_BASE_URL:-}" ]; then
  case "$ATS_TELEGRAM_API_BASE_URL" in
    http://*|https://*) ;;
    *) export ATS_TELEGRAM_API_BASE_URL="http://$ATS_TELEGRAM_API_BASE_URL" ;;
  esac
fi

_telegram_ready() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}${ATS_TELEGRAM_BOT_TOKEN:-}" ] \
    && [ -n "${TELEGRAM_ALLOWED_USER_IDS:-}${ATS_TELEGRAM_ALLOWED_USER_IDS:-}" ]
}

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
  if _telegram_ready; then
    export ATS_TELEGRAM_API_BASE_URL="${ATS_TELEGRAM_API_BASE_URL:-http://127.0.0.1:${port}}"
    export ATS_TELEGRAM_MODE="${ATS_TELEGRAM_MODE:-process}"
    python -m app.telegram --config "$ATS_CONFIG_PATH" &
    echo "ats-paper: telegram sidecar started pid $!" >&2
  else
    echo "ats-paper: telegram sidecar skipped — set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS on this service or in /app/var/telegram.env" >&2
  fi
  exec python -m app.cli serve --config "$ATS_CONFIG_PATH" --host 0.0.0.0 --port "$port"
fi

if [ "$role" = "telegram" ]; then
  export ATS_CONFIG_PATH="${ATS_CONFIG_PATH:-configs/paper.binance_public.usdt.yaml}"
  if ! _telegram_ready; then
    echo "ats-telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS missing; waiting" >&2
    while ! _telegram_ready; do
      sleep 15
    done
  fi
  exec python -m app.telegram --config "$ATS_CONFIG_PATH"
fi

exec "$@"
