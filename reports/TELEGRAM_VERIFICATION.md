# TELEGRAM_VERIFICATION

Дата: 2026-10-11T18:29:15.155803+00:00
Метод: только GET `http://127.0.0.1:8000/api/state` и те же функции, что рисует бот (`CommandService` + `views`).
Торговый движок, Telegram-процесс и 24-часовой монитор не останавливались. БД и конфиг торговли не менялись.
Экран телефона не открывался: сверка автоматическая по тому же коду, который отвечает на `/start` и остальные команды.

Итог: **PASSED** — 36/36 проверок.

## Снимок API

- health: mode=paper live_adapter=disabled loop=True ticks=408 telegram_in_api=False
- exchange=binance_public quote=USDT feed=binance_public
- news.source=MockNewsSource last_ok=2026-10-11T18:29:07.546739+00:00
- processes: paper=9659 telegram=40547 watch_24h=14595

| поле | baseline | ai |
| --- | --- | --- |
| starting | 1000 | 1000 |
| equity | 1000 | 1000 |
| free | 850 | 850 |
| reserved | 150 | 150 |
| profit_reserve | None | None |
| realized_pnl | 0 | 0 |
| unrealized_pnl | 0 | 0 |
| open_lots | 0 | 0 |
| completed_cycles | 0 | 0 |
| fills | 0 | 0 |
| resting buys | 3 | 3 |

## Проверки

| проверка | результат | детали |
| --- | --- | --- |
| `live_adapter_disabled` | PASSED | health.live_adapter=disabled live_trading=False state.live_trading=False |
| `mode_paper` | PASSED | health=paper state=paper |
| `market_source_binance_public` | PASSED | exchange=binance_public feed=binance_public start_has_binance_public=True |
| `news_marked_mock` | PASSED | news.source='MockNewsSource' |
| `telegram_controls_off` | PASSED | pause command stays read-only |
| `starting_1000_usdt_each` | PASSED | baseline.starting=1000 ai.starting=1000 quote=USDT |
| `start_free_reserved_reserve` | PASSED | screen=[Decimal('850.00'), Decimal('850.00'), Decimal('150.00'), Decimal('150.00')] api free/reserved/reserve baseline=(850,150,None) ai=(850,150,None) |
| `start_equity` | PASSED | screen=[Decimal('1000.00'), Decimal('1000.00')] api=(1000,1000) |
| `start_realized_unrealized` | PASSED | screen=[Decimal('0.00'), Decimal('0.00'), Decimal('0.00'), Decimal('0.00')] api baseline=(0,0) ai=(0,0) |
| `start_lots_and_cycles` | PASSED | screen=('0', '0', '0', '0') api lots=(0,0) cycles=(0,0) |
| `balance_sections_not_mixed` | PASSED | both arm headers present |
| `balance_grid_before_ai` | PASSED | Сетка block precedes Сетка + AI |
| `balance_baseline_matches_api` | PASSED | baseline equity/free/reserved/reserve/realized/unrealized screen=(1000.00,[Decimal('850.00'), Decimal('150.00'), Decimal('0.00')],[Decimal('0.00'), Decimal('0.00')]) api=(1000,850,150,None,0,0) |
| `balance_ai_matches_api` | PASSED | ai equity/free/reserved/reserve/realized/unrealized screen=(1000.00,[Decimal('850.00'), Decimal('150.00'), Decimal('0.00')],[Decimal('0.00'), Decimal('0.00')]) api=(1000,850,150,None,0,0) |
| `positions_sections_not_mixed` | PASSED | both arm headers present |
| `positions_baseline_lots` | PASSED | baseline api_open=0 listed=[] extra=[] missing=[] |
| `positions_baseline_resting_buys` | PASSED | baseline screen=['BTC', 'ETH', 'SOL'] api=['BTC', 'ETH', 'SOL'] |
| `positions_ai_lots` | PASSED | ai api_open=0 listed=[] extra=[] missing=[] |
| `positions_ai_resting_buys` | PASSED | ai screen=['BTC', 'ETH', 'SOL'] api=['BTC', 'ETH', 'SOL'] |
| `trades_baseline_fills` | PASSED | baseline screen buy/sell=0/0 api=0/0 empty=True |
| `trades_baseline_no_foreign_fill_ids` | PASSED | foreign ids on baseline screen: [] |
| `trades_ai_fills` | PASSED | ai screen buy/sell=0/0 api=0/0 empty=True |
| `trades_ai_no_foreign_fill_ids` | PASSED | foreign ids on ai screen: [] |
| `report_ab_equities` | PASSED | screen=[Decimal('1000.00'), Decimal('1000.00'), Decimal('0.00')] ab=(1000,1000,0) |
| `report_cycles_match_arms` | PASSED | cycles 0/0 lots 0/0 |
| `arms_currently_identical_values` | PASSED | baseline and AI cash/equity match (no AI-only news effect yet); sections still labeled separately |
| `process_paper_alive` | PASSED | pid=9659 |
| `process_telegram_alive` | PASSED | pid=40547 |
| `process_watch_24h_alive` | PASSED | pid=14595 |
| `paper_loop_running` | PASSED | ticks=408 loop=True |
| `telegram_is_separate_process` | PASSED | API process does not poll Telegram |
| `watch_24h_collecting` | PASSED | samples=43 last_at=2026-10-11T18:28:43.052880+00:00 age_sec=31 last_ticks=404 |
| `watch_24h_new_sample` | PASSED | wait skipped; last sample age_sec=31 samples=43 |
| `watch_24h_hourly_checkpoint_timing` | PASSED | elapsed_h=0.79 hour_files=0 (first checkpoint after 60 min) |
| `telegram_seen_state_tracks_fills` | PASSED | seen.ticks=408 api.ticks=408 seen.fills={'baseline': [], 'ai': []} api.fills baseline=[] ai=[] |
| `allowlist_blocks_other_ids` | PASSED | user 1 reply=Доступ запрещён. |

## Экраны (как их собрал бы бот с этого снимка)

### /start

```
AI Trading Control · PAPER · live отключён
🟢 Торговая система работает
Данные: binance_public · binance_public

Виртуальный баланс (USDT): 850.00 / 850.00 свободно, 150.00 / 150.00 в торговле, — / — Profit Reserve
Total equity: 1 000.00 / 1 000.00
Realized / Unrealized: 0.00 / 0.00 · AI 0.00 / 0.00
За 24 ч: 0.00 / 0.00
Открытые позиции: 0 / 0 · Закрытые циклы: 0 / 0
Активные монеты: BTC · ETH · SOL
Режим новостей: NORMAL · источник MOCK (реальные новости и LLM не подключены)

Пары чисел: Сетка / Сетка + AI.
```

### /balance

```
AI Trading Control · PAPER · live отключён
Баланс (USDT)

Сетка · equity 1 000.00
  free 850.00 · reserved 150.00 · Profit Reserve — · в позициях 0.00 (0.00%)
  realized 0.00 · unrealized 0.00 · комиссии 0
  за 24 ч 0.00 · drawdown 0.00% от пика 1 000.00

Сетка + AI · equity 1 000.00
  free 850.00 · reserved 150.00 · Profit Reserve — · в позициях 0.00 (0.00%)
  realized 0.00 · unrealized 0.00 · комиссии 0
  за 24 ч 0.00 · drawdown 0.00% от пика 1 000.00
```

### /positions

```
AI Trading Control · PAPER · live отключён
Открытые виртуальные лоты

Сетка: лотов 0 · в резерве 150.00 USDT
  ждут входа: BTC @ 83 024.97 (−1.01%), ETH @ 2 516.90 (−0.95%), SOL @ 110.1664 (−1.62%)

Сетка + AI: лотов 0 · в резерве 150.00 USDT
  ждут входа: BTC @ 83 024.97 (−1.01%), ETH @ 2 516.90 (−0.95%), SOL @ 110.1664 (−1.62%)
```

### /trades

```
AI Trading Control · PAPER · live отключён
Последние сделки

Сетка: fills 0, циклов 0, realized 0.00 USDT
  пока нет

Сетка + AI: fills 0, циклов 0, realized 0.00 USDT
  пока нет

Время исполнения = время тика; см. /api/decisions или журнал в дашборде.
```

### /report

```
AI Trading Control · PAPER · live отключён
A/B: сетка против сетки с AI-фильтром
Baseline equity 1 000.00 · AI equity 1 000.00 · Δ(AI−baseline) 0.00
Циклы: 0 / 0 · открытые лоты 0 / 0
Realized: 0.00 / 0.00 · drawdown 0.00% / 0.00%
Входы разрешено/запрещено: 6/1 · 6/1
Вето AI: 0 (спасло 0, упустило 0) · независимых событий 0
Причины отказов Сетка: max_active_pairs ×3278
Причины отказов Сетка + AI: max_active_pairs ×3278

Процесс работает 0 ч 47 мин · тиков 408 · ошибок 0
Новости: MOCK (реальные новости и LLM не подключены). Это проверка исправности, не прогноз доходности.
```

## Уведомления и дедуп

Живой Telegram-процесс не перезапускался. Файл `var/telegram_state.json` существует: seen.fills совпадают с API, `api_down=false`.
Рестарт без повтора истории проверен тестом `test_notifier_persists_seen_state_and_announces_only_what_it_missed` (изолированная БД, не `var/*.db`).

## Тесты интеграции

`pytest backend/tests/test_telegram.py` → **20 passed**. Временные SQLite в памяти, `var/local*.db` и конфиг торговли не трогались.

## Замечания

- `reports/PAPER_24H_REPORT.md` появится в конце 24 часов; почасовые `reports/hours/hour_NN.md` — после первого полного часа.
- Во время сверки jsonl вырос 41 → 42 сэмпл (`18:26:42Z` → `18:27:42Z`). Монитор пишет каждую минуту.
- Сейчас обе копии численно совпадают: сделок нет, mock-новости пустые. Смешение рук ловится структурой экранов (Сетка / Сетка + AI) и привязкой fill_id/лотов.
- Живой PaperRuntime стартовал до поля `profit_reserve`: в `/api/state` его нет (`null`), Telegram на `/start` показывает `— / —`. Это согласовано с API и не сдвиг цифр. После следующего рестарта движка (не сейчас) поле станет `0`.
- Прибыльность не оценивалась.
- Телефон не открывался. Сверьте `/start` в чате с блоком экранов выше — это тот же текст, который собирает бот.
