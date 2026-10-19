"""Runtime settings. Live trading cannot be turned on in this phase."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.money import QuoteCurrency, money
from app.ops.local_api import is_private_http_api_url, normalize_http_api_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: Literal["paper"] = "paper"
    exchange: str = "simulated"
    quote_currency: QuoteCurrency = QuoteCurrency.USDC
    report_currency: QuoteCurrency = QuoteCurrency.USDC
    watchlist: list[str] = Field(
        default_factory=lambda: ["BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "ADA", "AVAX"]
    )
    max_active_pairs: int = 3
    starting_virtual_balance: Decimal = Decimal("5000")
    per_order_quote: Decimal = Decimal("50")
    grid_step_pct: Decimal = Decimal("0.30")
    net_target_pct: Decimal = Decimal("0.10")
    max_portfolio_exposure_pct: Decimal = Decimal("40")
    max_per_asset_exposure_pct: Decimal = Decimal("15")
    max_open_lots_per_asset: int = 6
    min_order_book_depth_multiplier: Decimal = Decimal("10")
    allow_leverage: Literal[False] = False
    allow_margin: Literal[False] = False
    allow_live_trading: Literal[False] = False
    allow_withdraw: Literal[False] = False
    allow_shorts: Literal[False] = False
    news_poll_interval_sec: int = 60
    news_stale_after_sec: int = 600
    pause_new_buys_if_unverified_high_impact: bool = True
    assumed_maker_fee_pct: Decimal = Decimal("0.10")
    assumed_taker_fee_pct: Decimal = Decimal("0.10")
    assumed_slippage_pct: Decimal = Decimal("0.05")
    market_stale_after_sec: int = 15
    max_spread_pct: Decimal = Decimal("0.50")
    depeg_max_deviation_pct: Decimal = Decimal("1.00")
    max_drawdown_pct: Decimal = Decimal("10")
    profit_reserve_pct: Decimal = Decimal("0")
    database_url: str = "sqlite:///./local.db"
    config_path: str = "configs/paper.example.yaml"
    market_poll_interval_sec: int = 5
    run_loop: bool = False
    synthetic_seed: int = 7
    news_fixtures_path: str = ""
    cryptopanic_token: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    exchange_api_key: str = ""
    exchange_api_secret: str = ""
    exchange_api_passphrase: str = ""
    # Telegram. The token and the allowlist are accepted with or without the ATS_ prefix.
    telegram_bot_token: str = Field(default="", validation_alias=AliasChoices("ATS_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = Field(default="", validation_alias=AliasChoices("ATS_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"))
    telegram_allowed_user_ids: str = Field(
        default="", validation_alias=AliasChoices("ATS_TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_ALLOWED_USER_IDS")
    )
    telegram_digest_interval_sec: int = 3600
    telegram_max_messages_per_minute: int = 10
    # "process": the bot is a separate OS process talking to the local API (default).
    # "inprocess": the API process itself polls Telegram. Never both for one token.
    telegram_mode: Literal["process", "inprocess"] = "process"
    telegram_api_base_url: str = "http://127.0.0.1:8000"
    # Remote pause/halt/resume are off in the first version; reads only.
    telegram_controls_enabled: bool = False
    telegram_state_path: str = "var/telegram_state.json"
    # Drawdown level that triggers a one-time warning (below max_drawdown_pct, which blocks trading).
    telegram_drawdown_alert_pct: Decimal = Decimal("5")

    @field_validator(
        "starting_virtual_balance",
        "per_order_quote",
        "grid_step_pct",
        "net_target_pct",
        "max_portfolio_exposure_pct",
        "max_per_asset_exposure_pct",
        "min_order_book_depth_multiplier",
        "assumed_maker_fee_pct",
        "assumed_taker_fee_pct",
        "assumed_slippage_pct",
        "max_spread_pct",
        "depeg_max_deviation_pct",
        "max_drawdown_pct",
        "profit_reserve_pct",
        "telegram_drawdown_alert_pct",
        mode="before",
    )
    @classmethod
    def _decimals(cls, value: object) -> Decimal:
        return money(value)

    @field_validator("watchlist")
    @classmethod
    def _watchlist(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip().upper() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("watchlist must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _paper_only(self) -> Settings:
        if self.mode != "paper":
            raise ValueError("only paper mode is permitted in this phase")
        if self.allow_live_trading or self.allow_leverage or self.allow_margin:
            raise ValueError("leverage, margin, and live trading are disabled")
        if self.allow_withdraw or self.allow_shorts:
            raise ValueError("withdrawals and shorts are disabled")
        if self.quote_currency not in QuoteCurrency:
            raise ValueError("quote currency must be USDC, USDT, or EUR")
        if self.news_poll_interval_sec < 60:
            raise ValueError("news poll interval must be at least 60 seconds in this phase")
        if self.market_stale_after_sec <= 0 or self.news_stale_after_sec <= 0:
            raise ValueError("stale timeouts must be positive")
        if self.max_active_pairs < 1 or self.max_open_lots_per_asset < 1:
            raise ValueError("position caps must be positive")
        if self.market_poll_interval_sec < 1:
            raise ValueError("market poll interval must be positive")
        if self.exchange not in {"simulated", "binance_public", "disabled"}:
            raise ValueError("exchange must be 'simulated', 'binance_public' (read-only) or 'disabled'")
        if self.telegram_digest_interval_sec < 60 or self.telegram_max_messages_per_minute < 1:
            raise ValueError("telegram digest interval must be >= 60 s and the per-minute cap positive")
        self.telegram_user_ids  # validates the allowlist format
        object.__setattr__(self, "telegram_api_base_url", normalize_http_api_url(self.telegram_api_base_url))
        if not is_private_http_api_url(self.telegram_api_base_url):
            raise ValueError("telegram_api_base_url must point at loopback or a private host")
        if self.telegram_drawdown_alert_pct <= 0:
            raise ValueError("telegram_drawdown_alert_pct must be positive")
        if self.profit_reserve_pct < 0 or self.profit_reserve_pct > 100:
            raise ValueError("profit_reserve_pct must be between 0 and 100")
        return self

    @property
    def telegram_user_ids(self) -> frozenset[int]:
        ids: set[int] = set()
        for part in self.telegram_allowed_user_ids.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not part.lstrip("-").isdigit():
                raise ValueError("telegram_allowed_user_ids must be a comma-separated list of integers")
            ids.add(int(part))
        return frozenset(ids)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_user_ids)

    @property
    def telegram_inprocess(self) -> bool:
        return self.telegram_enabled and self.telegram_mode == "inprocess"

    @property
    def secrets_present(self) -> bool:
        return any(
            (
                self.cryptopanic_token,
                self.llm_api_key,
                self.exchange_api_key,
                self.exchange_api_secret,
                self.exchange_api_passphrase,
                self.telegram_bot_token,
            )
        )


def load_settings(path: str | Path | None = None) -> Settings:
    """YAML provides defaults. ATS_* environment variables override those keys."""

    config_path = Path(path) if path is not None else None
    payload: dict[str, object] = {}
    if config_path is not None:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("config file must be a mapping")
        payload = {
            key: value
            for key, value in raw.items()
            if f"ATS_{str(key).upper()}" not in os.environ
        }
    settings = Settings(**payload)
    if config_path is not None:
        settings = settings.model_copy(update={"config_path": str(config_path)})
    return settings
