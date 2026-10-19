# BACKTEST_REPORT

Live PaperRuntime и `var/local*.db` не использовались.

## Изолированная демонстрация полного цикла

Сценарий: якорь 100 → падение low 99.20 (BUY) → отскок high 101.20 (SELL).
Исполнение консервативное: сначала sell-фаза по high, затем buy-фаза по low; один бар не закрывает только что открытый лот.

- step 0.50%, adaptive=False
- buys=1 sells=1 cycles=1
- realized=0.126118493510132197500 equity=1000.126118493510132197500 fees={'USDT': Decimal('0.100126344538565302500')}
- free=950.126118493510132197500 reserved=50.000000000000000000000 profit_reserve=0 open_lots=0
- note: conservative: sell-phase then buy-phase; no same-bar round trip; not a live result

## Сравнение шагов на синтетической серии (не рынок Binance)

200 минутных баров с регулярным откатом ~0.7% и отскоком ~1.2%. Это проверка проводки, не оценка доходности на реальном рынке.
Режим эвристики (не прогноз): {'regime': 'UPTREND', 'reason': 'EMA12-26 1.40% and RSI 83.0', 'atr_pct': Decimal('0.6664135075428948609362119945'), 'ema_fast': Decimal('147.1236951428432453134487663'), 'ema_slow': Decimal('145.0988435289177474057620719'), 'rsi': Decimal('82.96288995238514445046903647'), 'realized_vol_pct': Decimal('0.5352490285715777218198009083'), 'forecast': False}
ATR%=0.6664135075428948609362119945 адаптивный шаг=0.67

| period | step | adaptive | equity | realized | cycles | buys | sells |
| --- | --- | --- | --- | --- | --- | --- | --- |
| train | 0.30 | False | 1000 | 0 | 0 | 0 | 0 |
| train | 0.50 | False | 1000.804055815936747000 | 0.804055815936747000 | 1 | 1 | 1 |
| train | 1.00 | False | 1000 | 0 | 0 | 0 | 0 |
| train | 0.67 | True | 1000.804055815936747000 | 0.804055815936747000 | 1 | 1 | 1 |
| test | 0.30 | False | 1000 | 0 | 0 | 0 | 0 |
| test | 0.50 | False | 1000.804055812343479835185267 | 0.80405581234347983518526708 | 1 | 1 | 1 |
| test | 1.00 | False | 1000 | 0 | 0 | 0 | 0 |
| test | 0.67 | True | 1000.804055812343479835185267 | 0.80405581234347983518526708 | 1 | 1 | 1 |

Indicators and replay equity are research, not a return forecast. Live paper params were not changed.
