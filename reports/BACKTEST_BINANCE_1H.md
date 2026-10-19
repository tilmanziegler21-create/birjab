# BACKTEST_REPORT

Символ BTC/USDT интервал 1h баров 8999.
Live PaperRuntime и его БД не использовались.
Режим (эвристика, не прогноз): {'regime': 'RANGE', 'reason': 'EMA12-26 0.14% ATR 0.15%', 'atr_pct': Decimal('0.1453587759361600858105588451'), 'ema_fast': Decimal('82997.08993095633129219985019'), 'ema_slow': Decimal('82882.25125048940945285424166'), 'rsi': Decimal('66.65826789288763840479290023'), 'realized_vol_pct': Decimal('0.08930355779785739939712761298'), 'forecast': False}
ATR%: 0.1453587759361600858105588451 адаптивный шаг: 0.30

| period | step | adaptive | equity | realized | cycles | buys | sells |
| --- | --- | --- | --- | --- | --- | --- | --- |
| train | 0.30 | False | 1000 | 0 | 0 | 0 | 0 |
| train | 0.50 | False | 1000.76471817022486270000000 | 0.76471817022486270000000 | 1 | 1 | 1 |
| train | 1.00 | False | 1000.76471817022486270000000 | 0.76471817022486270000000 | 1 | 1 | 1 |
| train | 0.30 | True | 1000 | 0 | 0 | 0 | 0 |
| test | 0.30 | False | 1000 | 0 | 0 | 0 | 0 |
| test | 0.50 | False | 1000.33039084072967875000000 | 0.33039084072967875000000 | 1 | 1 | 1 |
| test | 1.00 | False | 1000.53718368883528000000000 | 0.53718368883528000000000 | 1 | 1 | 1 |
| test | 0.30 | True | 1000 | 0 | 0 | 0 | 0 |

Indicators and replay equity are research, not a return forecast. Live paper params were not changed.
