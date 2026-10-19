from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.accounting.ledger import Ledger
from app.risk.manager import MarketContext, RiskPolicy
from app.schemas.orders import OrderIntent

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return NOW


def open_ledger(quote: str = "USDC", balance: str = "5000") -> Ledger:
    ledger = Ledger()
    ledger.open_account(quote, Decimal(balance))
    return ledger


def policy(**overrides: object) -> RiskPolicy:
    values: dict[str, object] = {
        "per_order_quote": Decimal("50"),
        "max_portfolio_exposure_pct": Decimal("40"),
        "max_per_asset_exposure_pct": Decimal("15"),
        "max_open_lots_per_asset": 6,
        "max_active_pairs": 3,
        "market_stale_after_sec": 15,
        "news_stale_after_sec": 600,
        "max_spread_pct": Decimal("0.50"),
        "depeg_max_deviation_pct": Decimal("1.00"),
        "max_drawdown_pct": Decimal("10"),
        "exit_fee_pct": Decimal("0.10"),
    }
    values.update(overrides)
    return RiskPolicy(**values)  # type: ignore[arg-type]


def context(now: datetime = NOW, **overrides: object) -> MarketContext:
    values: dict[str, object] = {
        "now": now,
        "feed_timestamp": now,
        "spread_pct": Decimal("0.01"),
        "quote_deviation_pct": Decimal("0"),
        "bids": {},
        "market_status_trading": True,
        "news_last_ok_at": now,
        "regime": "NORMAL",
    }
    values.update(overrides)
    if isinstance(values["regime"], str):
        from app.schemas.risk import RiskRegime

        values["regime"] = RiskRegime(values["regime"])
    return MarketContext(**values)  # type: ignore[arg-type]


def buy_intent(
    client_order_id: str = "buy-1",
    symbol: str = "BTC/USDC",
    quote_value: str = "50",
    strategy_id: str = "baseline_grid",
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        symbol=symbol,
        side="buy",
        purpose="entry",
        quote_value=Decimal(quote_value),
        reason="test entry",
        strategy_id=strategy_id,  # type: ignore[arg-type]
    )


def aged(now: datetime, seconds: int) -> datetime:
    return now - timedelta(seconds=seconds)
