# Перенос paper-торговли на Linux VPS

Для Render (private service + worker) см. `docs/RENDER.md`. Этот файл — свой Linux с systemd.

Реальный сервер из этого файла **не разворачивается**. Сначала бот и Telegram проверяются на Mac против уже идущего `PaperRuntime`. На VPS переезжают только после того, как `/start` в Telegram показывает те же балансы, что и `/api/state`.

`launchd` на Mac не держит процесс во сне и при выключении. 24/7 — это systemd на Linux.

LiveAdapter остаётся выключенным. API и SQLite слушают только `127.0.0.1`.

## Что уже есть в репозитории

| путь | роль |
| --- | --- |
| `deploy/systemd/ats-paper.service` | торговый движок, `Restart=always`, SIGTERM → `runtime.persist()` |
| `deploy/systemd/ats-telegram.service` | отдельный read-only бот; **не** `Requires=` paper, чтобы при падении движка бот успел написать «API недоступен» |
| `deploy/systemd/ats-health.timer` | раз в минуту `python -m app.ops.health` на `http://127.0.0.1:8000/health` |
| `deploy/systemd/ats-backup.timer` | ежедневная копия `var/*.db` + replay снимка |
| `deploy/logrotate/ats` | ротация `var/*.log` |
| `python -m app.cli backup` | ручная копия и проверка восстановления |

Два `serve` на одну `database_url` не стартуют: `InstanceLock` в `var/paper.<hash>.lock`. Telegram сам по себе держит `var/telegram.<hash>.lock`.

## Этап 1 — Mac, без остановки текущего процесса

1. Не запускать второй `python -m app.cli serve` и не удалять `var/*.db`.
2. Убедиться, что API отвечает только локально: `curl -s http://127.0.0.1:8000/health` → `live_adapter: disabled`, `loop: true`.
3. Поднять **только** Telegram-процесс (`python -m app.telegram`). Написать боту `/whoami`, вписать числовой id в `.env`, перезапустить **только** Telegram, проверить `/start`, `/balance`, `/news`.
4. `/news` должен содержать `MOCK` / `TEST/MOCK`, источник, ссылку, время публикации и получения. Это не настоящий ИИ.
5. Снять контрольную копию, не трогая живые файлы как единственный оригинал:

   ```bash
   python -m app.cli backup --config configs/paper.binance_public.usdt.yaml --dest var/backups
   ```

   Команда копирует `var/local.db`, `var/local.baseline.db`, `var/local.ai.db` и прогоняет replay. Живой процесс не останавливается.

## Этап 2 — перенос истории на VPS

1. Linux x86_64, Python 3.11+, пользователь `ats`, каталог `/opt/botbirja`.
2. Остановить **только** наблюдение на Mac, когда будете готовы переключить движок. Скопировать репозиторий, `.venv`, `.env` и каталог `var/` (все `*.db`, `telegram_state.json`, логи). Без `var/*.db` история обнулится.
3. На сервере:

   ```bash
   python3 -m venv /opt/botbirja/.venv
   /opt/botbirja/.venv/bin/pip install -e "/opt/botbirja[dev]"
   cd /opt/botbirja/backend
   alembic -x url=sqlite:////opt/botbirja/var/local.db upgrade head
   alembic -x url=sqlite:////opt/botbirja/var/local.baseline.db upgrade head
   alembic -x url=sqlite:////opt/botbirja/var/local.ai.db upgrade head
   ```

   `init_db` также добавит колонку `profit_reserve`, если её ещё нет. Старые события `SELL_FILL` повторно резерв не снимают.

4. Проверить копию **до** старта движка:

   ```bash
   /opt/botbirja/.venv/bin/python -m app.cli backup \
     --config /opt/botbirja/configs/paper.binance_public.usdt.yaml \
     --dest /opt/botbirja/var/backups
   ```

   В JSON `verify` те же `free` / `realized_pnl`, что на Mac. Если нет — не включать `ats-paper`.

5. Юниты:

   ```bash
   sudo cp deploy/systemd/ats-*.service deploy/systemd/ats-*.timer /etc/systemd/system/
   sudo cp deploy/logrotate/ats /etc/logrotate.d/ats
   sudo systemctl daemon-reload
   sudo systemctl enable --now ats-paper.service
   sudo systemctl enable --now ats-telegram.service
   sudo systemctl enable --now ats-health.timer ats-backup.timer
   ```

6. Проверки:

   ```bash
   curl -s http://127.0.0.1:8000/health
   /opt/botbirja/.venv/bin/python -m app.ops.health --max-lag 60
   journalctl -u ats-paper -u ats-telegram -n 50 --no-pager
   ```

   `live_adapter` должен быть `disabled`. Порт 8000 не публиковать (`--host 127.0.0.1`). SQLite не класть на NFS/публичный диск.

7. Telegram `/start` и `/balance` после переезда: те же equity, realized, Profit Reserve, число циклов, что в последнем backup. Если цифры другие — остановить `ats-paper` и сравнить `var/*.db` с копией Mac.

8. Mac-процесс после успешного переезда **выключить**, иначе два движка разъедут историю. На время копирования держать Mac-движок, на VPS ещё не стартовать.

## Сбой и восстановление

- Падение `ats-paper`: systemd поднимает снова; Telegram пишет «локальный API недоступен», затем «восстановлена».
- SIGTERM / `systemctl stop`: lifespan вызывает `runtime.persist()`.
- Повреждённый снимок: `load()` ставит `balance_snapshot_mismatch` и блокирует торговлю. Восстанавливать из `var/backups/<UTC>/`, не править баланс руками.
- Второй `serve` на ту же БД завершится с текстом про lock.

## Чего нет

- Публичный bind API или проброс SQLite наружу.
- Включение LiveAdapter.
- Настоящий LLM и новостные API: в Telegram они по-прежнему помечены как MOCK.
- Автоматический деплой из этой инструкции: сервер поднимает оператор.
