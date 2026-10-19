# Проблемы и предложения

1. Пассивность live-сетки ожидаема: рынок с старта прогона в основном рос, лимит на 0,50% ниже якоря. Это не сломанный бот.
2. Intra-bar replay намеренно консервативен (сначала SELL по high, потом BUY по low, нет кругa в одном баре). Оптимистичная модель завысила бы циклы.
3. Для точного fill нужны сделки/стакан, не только OHLC. Следующий шаг архива — `aggTrades` с Vision, не смешивать с live DB.
4. Адаптивный шаг — только research. В live его нет.
5. Telegram Mini App отложен: нужен HTTPS и проверка initData; иначе API/БД окажутся в сети.
6. После полного `market-sync` пересчитать `replay` на 1h/5m BTC и ETH. Не трогать текущий 24-часовой прогон.
7. Реальные новости и LLM — следующая большая задача, отдельно от этой.

## Запуск

```bash
# живая панель (процесс уже может быть запущен)
python -m app.telegram

# история
python -m app.cli market-sync --dest var/market/candles.db

# офлайн сравнение, чужая БД
python -m app.cli replay --candles var/market/candles.db --symbol BTC/USDT --interval 1h
```

Не запускать второй `serve`. LiveAdapter выключен.
