# Historical data quality report

Источник: [Binance Vision](https://data.binance.vision/) (`data.binance.vision/data/spot/...`), только GET, без ключа.

Целевой набор (загрузка запущена фоном в `var/market/sync.log`, PID в `var/market/sync.pid`):

| интервал | период | пары |
| --- | --- | --- |
| 1m | 90 дней | BTC/ETH/SOL/XRP USDT |
| 5m | 180 дней | то же |
| 1h | 365 дней | то же |

Хранилище: `var/market/candles.db` (не ledger). Дедуп по `(symbol, interval, open_time)`. Пропуски: `CandleStore.gaps`. Метки времени UTC; µs/ns в архиве приводятся к миллисекундам.

Повтор: `python -m app.cli market-sync --dest var/market/candles.db`

Фактическое покрытие на момент отчёта (синк ещё идёт по ETH/SOL/XRP):

| пара | интервал | баров | с | по | пропуски |
| --- | --- | --- | --- | --- | --- |
| BTC/USDT | 1m | 146880 | 2026-07-01 | 2026-10-10 | 0 |
| BTC/USDT | 5m | 55584 | 2026-04-01 | 2026-10-10 | 0 |
| BTC/USDT | 1h | 8856 | 2025-10-01 | 2026-10-04 | 0 |

`failed: 1` на каждом наборе — обычно сегодняшний незакрытый daily zip. Это не дыра внутри закрытых месяцев.

Покрытие после синка смотреть так:

```bash
.venv/bin/python - <<'PY'
from app.market.history import CandleStore
s = CandleStore("var/market/candles.db")
for sym in ("BTC/USDT","ETH/USDT","SOL/USDT","XRP/USDT"):
    for iv in ("1m","5m","1h"):
        print(s.coverage(sym, iv))
s.close()
PY
```

Индикаторы ATR / EMA / RSI / realized vol и класс RANGE / UPTREND / DOWNTREND / HIGH_VOLATILITY — эвристика, **не прогноз**.

Пока архив качается, живая панель берёт короткие Δ из публичного `/api/v3/klines`.
