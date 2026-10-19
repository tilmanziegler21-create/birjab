# AUDIT_REPORT — готовность к paper-запуску на реальных котировках

Дата: 2026-10-11. Ветка `main`, коммитов нет (всё в рабочем дереве). Python 3.13.1, venv `.venv`.

## 1. Тесты

`.venv/bin/pytest --override-ini addopts= -q` → **79 passed**, 1 warning (deprecation Starlette/httpx, не наш код).

| файл | тестов | что покрывает |
| --- | --- | --- |
| test_ledger | 13 | лоты, free/reserved, идемпотентность, частичные fill, комиссии, equity |
| test_risk | 9 | лимиты, drawdown, spread/peg/stale, LiveAdapter не шлёт |
| test_safety | 7 | LiveAdapter без ключей и без отправки, live-флаг отклоняется, в дереве нет секретов |
| test_paper_grid | 8 | матчинг по стакану, сетка, выходы, комиссии на продаже |
| test_persistence | 2 | сохранение/восстановление ledger, несовпадение снапшота → блокировка |
| test_news_and_universe | 4 | hard-filter вселенной, режимы |
| test_news_pipeline | 14 | дедуп, сущности, затухание, статус площадки, fail-closed |
| test_public_ab | 10 | allowlist публичных URL, парсеры, A/B, stale-книга |
| test_stress | 12 | крах −15%, drawdown, ложный твит, взлом, outage, depeg, делистинг, рестарт и т.д. |

## 2. Проверки на реальных данных (выполнены сегодня, Binance public, только GET)

Конфиг проверки: `exchange: binance_public`, `quote_currency: USDT`, watchlist BTC/ETH/SOL/XRP, `max_active_pairs: 4`, депозит 5000, лот 50, шаг 1,00%, `max_portfolio_exposure_pct: 30`.

| проверка | результат |
| --- | --- |
| Котировки BTC/ETH/SOL/XRP к USDT | получены: bid/ask, spread 0,00–0,009%, глубина 0,36–1,57 млн USDT, status `trading`, tick/lot/minNotional из exchangeInfo |
| Запрос к `/api/v3/order`, `/account`, depth с `signature=` | отклонены `PublicBookError` до сетевого вызова |
| Peg-прокси USDC/USDT | 0,0915% (< порога 1,00%) |
| 3 тика рантайма | 4/4 символа ok, ошибок 0, 1,3–3,2 с на тик (последовательные HTTP) |
| Выставление сетки | обе копии: 4 отдыхающих buy, free 4800 / reserved 200, equity 5000 |
| Рестарт из SQLite | те же `client_order_id`, free/reserved совпали, `trading_blocked=False` |
| `python -m app.cli serve` + фоновый цикл | /health `loop:true`, тики идут каждые ~5,5 с |
| 100 параллельных `GET /api/state` во время тиков | 100 × 200, ошибок в логе 0 |
| `POST /api/tick` при работающем цикле | 409 |
| `POST /api/controls/liquidate` без фразы | 400 |
| `/api/order` | GET 404, POST 405 (маршрута нет; 405 даёт StaticFiles-mount на `/`) |
| JSON состояния | строк `api_key`/`secret`/`token` нет |
| Дашборд `/` | рендерится на реальных данных, кнопки работают, RESUME/LIQUIDATE заблокированы корректно |
| Логи решений в stderr при `app.cli serve` | есть (JSON-строки `"allowed":true,"reason":"reserved"`) |

Live: `LiveAdapter.place_order()` всегда поднимает `LiveTradingDisabledError`, ключи не хранятся; `allow_live_trading` типа `Literal[False]` — валидатор отклоняет `true`. Файл `configs/live.DISABLED.example.yaml` имеет `exchange: disabled`, из него рантайм собрать нельзя.

## 3. Найденные проблемы

### Блокирующих для paper-запуска нет. Для суточной статистики важны п. 3.1–3.3.

**3.1. Журнал решений и история equity не сохраняются (средняя важность).**
`decision_log` живёт в памяти (лимит 2000 строк), в БД пишутся только события ledger, лоты и новости. После рестарта `/api/decisions` пуст (проверено: 0 записей при перезапуске поверх существующих БД), `entries_allowed/rejected`, `reject_reasons`, `paused_entry_pct` и A/B `ai_vetoes` начинают с нуля. Временного ряда equity нет вообще — только текущее значение и `peak_equity`. Для «собирать статистику 24/7» нужно как минимум: таблица решений (append) и периодический снимок equity/exposure обеих копий.
Что сохраняется и переживает рестарт: все fills, лоты, realized PnL, комиссии, резервы, новости.

**3.2. Зависшую цену по REST не отличить от живой (средняя важность).**
`PublicBookClient._fetch_book` ставит `updated_at = received_at`, поэтому флаг `stale` и правило `market_stale_after_sec` для `binance_public` никогда не срабатывают: при REST-опросе Binance не отдаёт timestamp стакана. Если API начнёт отдавать одинаковые книги, сетка продолжит матчиться по ним. Сетевые ошибки и HTTP-сбои ловятся (символ пропускается, обе копии замирают — покрыто stress-тестом outage). Решение: сравнивать `lastUpdateId` между опросами и считать книгу устаревшей, если он не меняется дольше `market_stale_after_sec`.

**3.3. Нет супервизора процесса и ротации логов (для 24/7).**
`python -m app.cli serve` — один процесс, при падении/перезагрузке Mac никто не поднимет. Сам цикл устойчив: исключение в тике логируется и цикл продолжается (`_loop`), рестарт восстанавливает состояние. Нужен `launchd`-plist (или `nohup` + ручной контроль) и перенаправление stderr в файл; ротации нет, при ~8 строках лога на тик файл растёт ~10 МБ/сутки с access-логом uvicorn.

**3.4. Чтение состояния из API без блокировки (низкая важность).**
Тик выполняется в executor-потоке, `snapshot()` — в threadpool FastAPI; `dict`'ы лотов/ордеров читаются во время их изменения. За 100 параллельных запросов сбоев не было, но теоретически возможен редкий 500 на `/api/state` («dictionary changed size during iteration»). На торговый цикл не влияет. Решение: `threading.Lock` вокруг `tick()` и `snapshot()`.

**3.5. Лимитная цена не квантована под `tick_size` (низкая, известная).**
Отдыхающий buy BTC стоял по `82303.9866`, биржа приняла бы только `82303.98`. В paper-матчинге это не мешает, но PnL отличается от реального на доли тика.

**3.6. Длительность тика.** 4 пары → ~1,3 с, 8 пар → ~2,7 с последовательных GET; фактический интервал = 5 с сна + тик. Лимиты Binance (weight ≈ 5 на depth-20) не близко.

**3.7. Организационное.** Репозиторий без единого коммита; `frontend/dist` в `.gitignore`, поэтому на чистом клоне перед запуском нужен `npm run build`. Docker/Compose/Postgres не запускались (нет Docker).

## 4. Что ещё не проверено на реальной ленте

- Полный цикл buy → sell на реальных котировках: за время аудита цена не дошла до уровня −1,00%, выходов и завершённых сделок не было. Учёт комиссий на продаже подтверждён только тестами (`test_paper_grid`, `test_ledger`).
- Поведение при длительном обрыве сети (часы) — только stress-тест на несколько тиков.
- Переход суток/недели по памяти: `ledger.fills` и `ledger.events` растут неограниченно в памяти и `save()` каждые 5 с перечитывает все `event_id` — при сотнях сделок в сутки это не проблема, при десятках тысяч понадобится инкрементальный save.

## 5. Исправлено после аудита (тот же день, 82 теста проходят)

- **3.1 закрыт.** Журнал решений пишется в `risk_decisions` в БД копии (append + обновление счётчика `repeats`), при старте последние 2000 строк загружаются обратно, поэтому `entries_allowed/rejected`, `reject_reasons` и A/B-вето продолжаются после рестарта. Каждый тик в `equity_snapshots` каждой копии пишется строка: equity, cash, free/reserved, gross position, realized/unrealized, exposure %, drawdown %, open lots, cycles. Чтение: `GET /api/equity?limit=N`. Миграция `0003_decision_log_equity`. Тест `test_decision_log_and_equity_history_survive_restart`.
- **3.2 закрыт.** `parse_depth` читает `lastUpdateId`; `PublicFeed` держит время первого появления каждого id и подставляет его в `updated_at`, пока id не меняется. Через `market_stale_after_sec` (15 с) замёрзшая книга получает `stale: true`, RiskManager отклоняет входы с `stale_market_feed`; новый id — снова живая книга. Тест `test_frozen_public_book_is_stale_by_update_id_and_recovers`.
- **Найден и исправлен попутно: после HALT ALL → RESUME пара не входила заново.** Отменённая покупка сохраняла свой `client_order_id` (`…-buy-{level}`), стратегия предлагала тот же id, адаптер и ledger отвечали «idempotent», новой заявки не появлялось никогда (и после рестарта тоже — резерв с этим id уже освобождён). Теперь `GridStrategy` пропускает потраченные id и берёт `…-buy-{level}-r{n}`. Тест `test_halt_then_resume_re_enters_under_a_fresh_id_also_after_restart`. Это изменение поведения существующего кода; старые тесты проходят без правок.
- `make_engine` создаёт каталог для SQLite-файла, чтобы `database_url: sqlite:///./var/local.db` работал без ручного `mkdir`.

Не трогались: 3.3 (супервизор/ротация — отложено по решению), 3.4 (lock вокруг snapshot), 3.5 (квантование цены под tick_size).

## 6. Запуск 2026-10-11 17:28 UTC+2

Конфиг `configs/paper.binance_public.usdt.yaml`: 1 000 USDT, шаг 0,50%, BTC/ETH/SOL/XRP к USDT, активных пар ≤ 3, лот 50, экспозиция ≤ 30% портфеля и ≤ 15% на актив, комиссии 0,10/0,10, slippage 0,05. Процесс: `python -m app.cli serve --config configs/paper.binance_public.usdt.yaml --port 8000`, отдельная сессия, PID в `var/serve.pid`, лог `var/serve.log`, БД `var/local*.db`. Через минуту: 3 отдыхающих покупки у каждой копии (XRP отклонён `max_active_pairs`), free 850 / reserved 150, ошибок 0, `/api/equity` пишется. Запуск пережил один принудительный рестарт процесса — журнал решений и резервы восстановились.

## 7. Вывод

Paper-запуск на реальных котировках идёт. Движок, риск, персистентность и дашборд работают на живых данных, live отключён и это проверено тестами. До того как оставлять процесс без присмотра надолго, нужен п. 3.3 (launchd + ротация лога): сейчас при перезагрузке Mac или закрытии сессии процесс не поднимется сам, хотя состояние после ручного перезапуска восстанавливается полностью.
