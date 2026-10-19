# Paper на Render (24/7)

Локальный Mac-процесс **выключен**. История на момент остановки: equity **1000.08 USDT**, 1 закрытый круг SOL, A/B одинаковые, live выключен. Снимок: `var/backups/20261012T122838Z/` и архив `var/backups/render-handoff.tgz`.

LiveAdapter не включается. Ключей биржи нет. API снаружи не публикуется: `ats-paper` — private service, Telegram — worker. Бесплатный Web на Render засыпает и для торговли не годится.

## Что поднимается

| сервис | тип | диск | команда |
| --- | --- | --- | --- |
| `ats-paper` | private (`pserv`) | `/app/var` → SQLite | `paper` |
| `ats-telegram` | worker | `/app/var` → `telegram_state.json` | `telegram` |

Оба из одного `Dockerfile`, регион Frankfurt, план Starter, 1 инстанс (диск иначе нельзя). SIGTERM даёт `runtime.persist()`. Второй движок на ту же БД не стартует (`InstanceLock`).

Пока на диске нет `local.db`, paper **ждёт** и тики не идут. Пустой диск иначе создал бы новый счёт на 1000 USDT и стёр бы ночную историю.

## Перед деплоем

1. Репозиторий на GitHub/GitLab (сейчас remote нет). Секреты и `var/` в git не класть.
2. Render → New → Blueprint → этот репозиторий, файл `render.yaml`.
3. Для `ats-telegram` вписать `TELEGRAM_BOT_TOKEN` и `TELEGRAM_ALLOWED_USER_IDS` (тот же id, что на Mac).
4. Не ставить `ATS_EXCHANGE_API_KEY`, `ATS_LLM_API_KEY`, `ATS_ALLOW_LIVE_TRADING=true`.

## Восстановить БД (обязательно до первого тика)

После первого деплоя paper пишет в лог `waiting for /app/var/local.db`. Инстанс при этом уже запущен — SSH именно на него, **не** `--ephemeral` (у ephemeral нет диска).

С Mac, после `render ssh` / SCP из дашборда (Connect → SSH):

```bash
scp -s var/backups/render-handoff.tgz \
  SERVICE@ssh.frankfurt.render.com:/app/var/render-handoff.tgz
render ssh ats-paper
# на сервере:
cd /app/var && tar -xzf render-handoff.tgz \
  local.db local.baseline.db local.ai.db
ls -l local.db local.baseline.db local.ai.db
```

Проверка файлов: `local.db`, `local.baseline.db`, `local.ai.db`. Paper сам выйдет из ожидания и продолжит тот же ledger.

Состояние Telegram — отдельно, на диск воркера (`telegram_state.json` из того же архива в `/app/var` у `ats-telegram`). Без этого панели и last-seen сбросятся, баланс торговли не затронется.

## Проверка после старта

- Лог paper: `live_trading=false`, `loop` идёт, ticks растут.
- Telegram `/start`: equity **1000.08**, свободно ~850.08, в заявках 150, циклов 1. Если 1000.00 и 0 циклов — на диске чужой/пустой sqlite, **останови paper** и положи файлы из `20261012T122838Z`.
- Два сервиса `ats-paper` не запускать.
- Mac `serve` больше не поднимать: два движка разъедут историю.

## Чего нет

- Публичный URL дашборда и порт 8000 с интернета.
- Автозаливки sqlite при деплое (диск недоступен на build/pre-deploy).
- Настоящих новостей и LLM: в боте по-прежнему MOCK.
- Включения LiveAdapter.
