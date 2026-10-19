"""Risk manager. It reserves cash or base. It does not send orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.accounting.ledger import (
    IdempotencyConflict,
    InsufficientLotQuantity,
    InvalidFill,
    Ledger,
    LedgerHalted,
    LedgerStateError,
    OutOfFunds,
)
from app.accounting.pnl import MissingMark
from app.money import money
from app.risk.limits import drawdown_exceeds, exceeds_cap
from app.schemas.orders import OrderIntent
from app.schemas.risk import RiskDecision, RiskRegime

_NEWS_BUY_BLOCKS = {
    RiskRegime.HIGH_RISK,
    RiskRegime.PAUSE_BUYS,
    RiskRegime.HALT_ALL,
    RiskRegime.DEPEG,
    RiskRegime.EXCHANGE_ISSUE,
}
_NEWS_SELL_BLOCKS = {RiskRegime.HALT_ALL, RiskRegime.EXCHANGE_ISSUE}


@dataclass
class MarketContext:
    now: datetime
    feed_timestamp: datetime | None
    spread_pct: Decimal | None
    quote_deviation_pct: Decimal | None
    bids: dict[str, Decimal] = field(default_factory=dict)
    market_status_trading: bool = True
    news_last_ok_at: datetime | None = None
    regime: RiskRegime = RiskRegime.NORMAL
    size_multiplier: Decimal | None = None
    manual_pause_buys: bool = False
    manual_halt: bool = False
    emergency_confirmed: bool = False
    pair_allowed: bool = True


@dataclass(frozen=True)
class RiskPolicy:
    per_order_quote: Decimal
    max_portfolio_exposure_pct: Decimal
    max_per_asset_exposure_pct: Decimal
    max_open_lots_per_asset: int
    max_active_pairs: int
    market_stale_after_sec: int
    news_stale_after_sec: int
    max_spread_pct: Decimal
    depeg_max_deviation_pct: Decimal
    max_drawdown_pct: Decimal
    exit_fee_pct: Decimal


class RiskManager:
    def __init__(self, ledger: Ledger, policy: RiskPolicy) -> None:
        self.ledger = ledger
        self.policy = policy
        self.decisions: list[RiskDecision] = []

    def authorize(self, intent: OrderIntent, ctx: MarketContext) -> RiskDecision:
        if intent.side == "buy":
            decision = self._authorize_buy(intent, ctx)
        else:
            decision = self._authorize_sell(intent, ctx)
        self.decisions.append(decision)
        return decision

    def _authorize_buy(self, intent: OrderIntent, ctx: MarketContext) -> RiskDecision:
        with self.ledger.exclusive():
            if self.ledger.trading_blocked:
                return self._reject(intent, self.ledger.block_reason or "ledger_blocked", ctx.regime)
            existing = self.ledger.reservations.get(intent.client_order_id)
            if existing is not None and existing.status == "active":
                return self._allow(
                    intent,
                    "idempotent",
                    ctx.regime,
                    reservation_id=existing.reservation_id,
                    reserved_quote=existing.remaining,
                )
            if existing is not None:
                return self._reject(intent, "reservation_not_reusable", ctx.regime)
            reason = self._buy_reject_reason(intent, ctx)
            if reason is not None:
                return self._reject(intent, reason, ctx.regime)
            quote_value = self._sized_quote(intent, ctx)
            if isinstance(quote_value, str):
                return self._reject(intent, quote_value, ctx.regime)
            try:
                self.ledger.reserve(intent.client_order_id, intent.symbol, quote_value)
            except OutOfFunds:
                return self._reject(intent, "out_of_funds", ctx.regime)
            except LedgerHalted as exc:
                return self._reject(intent, str(exc), ctx.regime)
            except (IdempotencyConflict, InvalidFill) as exc:
                return self._reject(intent, str(exc), ctx.regime)
            return self._allow(
                intent,
                "reserved",
                ctx.regime,
                reservation_id=intent.client_order_id,
                reserved_quote=quote_value,
            )

    def _authorize_sell(self, intent: OrderIntent, ctx: MarketContext) -> RiskDecision:
        with self.ledger.exclusive():
            if self.ledger.trading_blocked:
                return self._reject(intent, self.ledger.block_reason or "ledger_blocked", ctx.regime)
            existing = self.ledger.holds.get(intent.client_order_id)
            if existing is not None and existing.status == "active":
                return self._allow(intent, "idempotent", ctx.regime)
            if existing is not None:
                return self._reject(intent, "hold_not_reusable", ctx.regime)
            reason = self._sell_reject_reason(intent, ctx)
            if reason is not None:
                return self._reject(intent, reason, ctx.regime)
            try:
                self.ledger.hold_base(intent.client_order_id, intent.lot_id or "", intent.quantity or Decimal("0"))
            except InsufficientLotQuantity:
                return self._reject(intent, "insufficient_lot_quantity", ctx.regime)
            except LedgerHalted as exc:
                return self._reject(intent, str(exc), ctx.regime)
            except (IdempotencyConflict, InvalidFill) as exc:
                return self._reject(intent, str(exc), ctx.regime)
            return self._allow(intent, "base_held", ctx.regime)

    def _buy_reject_reason(self, intent: OrderIntent, ctx: MarketContext) -> str | None:
        if intent.strategy_id == "emergency":
            return "emergency_strategy_cannot_buy"
        if not ctx.pair_allowed:
            return "pair_blocked"
        feed = self._feed_reject_reason(ctx)
        if feed is not None:
            return feed
        if ctx.manual_halt:
            return "manual_halt"
        if ctx.manual_pause_buys:
            return "manual_pause_buys"
        peg = self._peg_reject_reason(ctx)
        if peg is not None:
            return peg
        if self._drawdown_reject_reason(ctx) is not None:
            return self._drawdown_reject_reason(ctx)
        if intent.strategy_id == "ai_filtered_grid":
            news = self._news_reject_reason(ctx, blocks=_NEWS_BUY_BLOCKS)
            if news is not None:
                return news
        sized = self._sized_quote(intent, ctx)
        if isinstance(sized, str):
            return sized
        if sized > self.policy.per_order_quote:
            return "order_exceeds_per_order_quote"
        if intent.symbol not in self.ledger.active_symbols():
            if len(self.ledger.active_symbols()) >= self.policy.max_active_pairs:
                return "max_active_pairs"
        pending = sum(
            1
            for item in self.ledger.reservations.values()
            if item.status == "active" and item.side == "buy" and item.symbol == intent.symbol
        )
        if self.ledger.open_lot_count(intent.symbol) + pending >= self.policy.max_open_lots_per_asset:
            return "max_open_lots"
        equity = self._equity(ctx)
        if isinstance(equity, str):
            return equity
        bid = ctx.bids.get(intent.symbol, Decimal("0"))
        asset_value = self.ledger.gross_position_value(intent.symbol, bid) + self.ledger.active_buy_quote(
            intent.symbol
        )
        portfolio_value = Decimal("0")
        for symbol in self.ledger.active_symbols():
            symbol_bid = ctx.bids.get(symbol)
            if symbol_bid is None:
                if any(lot.symbol == symbol and lot.remaining_base_qty > 0 for lot in self.ledger.lots.values()):
                    return "missing_mark"
                portfolio_value += self.ledger.active_buy_quote(symbol)
            else:
                portfolio_value += self.ledger.gross_position_value(symbol, symbol_bid)
                portfolio_value += self.ledger.active_buy_quote(symbol)
        if exceeds_cap(
            value=asset_value + sized,
            equity=equity,
            cap_pct=self.policy.max_per_asset_exposure_pct,
        ):
            return "per_asset_exposure"
        if exceeds_cap(
            value=portfolio_value + sized,
            equity=equity,
            cap_pct=self.policy.max_portfolio_exposure_pct,
        ):
            return "portfolio_exposure"
        return None

    def _sell_reject_reason(self, intent: OrderIntent, ctx: MarketContext) -> str | None:
        feed = self._feed_reject_reason(ctx)
        if feed is not None:
            return feed
        emergency = intent.purpose == "emergency_exit" and ctx.emergency_confirmed
        if ctx.manual_halt and not emergency:
            return "manual_halt"
        if intent.purpose == "emergency_exit" and not ctx.emergency_confirmed:
            return "emergency_not_confirmed"
        if (
            intent.strategy_id == "ai_filtered_grid"
            and ctx.regime in _NEWS_SELL_BLOCKS
            and not emergency
        ):
            return "news_blocks_exit"
        return None

    def _feed_reject_reason(self, ctx: MarketContext) -> str | None:
        if not ctx.market_status_trading:
            return "market_not_trading"
        if ctx.feed_timestamp is None or ctx.now.tzinfo is None or ctx.feed_timestamp.tzinfo is None:
            return "missing_market_feed" if ctx.feed_timestamp is None else "naive_timestamp"
        age = (ctx.now - ctx.feed_timestamp).total_seconds()
        if age > self.policy.market_stale_after_sec:
            return "stale_market_feed"
        if ctx.spread_pct is None:
            return "missing_spread"
        if money(ctx.spread_pct) > self.policy.max_spread_pct:
            return "spread_too_wide"
        return None

    def _peg_reject_reason(self, ctx: MarketContext) -> str | None:
        if ctx.quote_deviation_pct is None:
            return "quote_peg_unknown"
        if abs(money(ctx.quote_deviation_pct)) > self.policy.depeg_max_deviation_pct:
            return "depeg"
        return None

    def _drawdown_reject_reason(self, ctx: MarketContext) -> str | None:
        equity = self._equity(ctx)
        if isinstance(equity, str):
            return equity
        if drawdown_exceeds(peak=self.ledger.peak_equity, current=equity, cap_pct=self.policy.max_drawdown_pct):
            return "max_drawdown"
        return None

    def _news_reject_reason(self, ctx: MarketContext, *, blocks: set[RiskRegime]) -> str | None:
        if ctx.news_last_ok_at is None:
            return "news_unavailable"
        if ctx.news_last_ok_at.tzinfo is None:
            return "naive_timestamp"
        age = (ctx.now - ctx.news_last_ok_at).total_seconds()
        if age > self.policy.news_stale_after_sec:
            return "news_stale"
        if ctx.regime == RiskRegime.CAUTION and ctx.size_multiplier is None:
            return "reduce_size_without_multiplier"
        if ctx.regime in blocks:
            return f"news_regime_{ctx.regime.value.lower()}"
        return None

    def _sized_quote(self, intent: OrderIntent, ctx: MarketContext) -> Decimal | str:
        quote_value = money(intent.quote_value or Decimal("0"))
        if intent.strategy_id == "ai_filtered_grid" and ctx.regime == RiskRegime.CAUTION:
            if ctx.size_multiplier is None:
                return "reduce_size_without_multiplier"
            quote_value = quote_value * money(ctx.size_multiplier)
            if quote_value <= 0:
                return "reduce_size_without_multiplier"
        return quote_value

    def _equity(self, ctx: MarketContext) -> Decimal | str:
        try:
            breakdown = self.ledger.mark_equity(ctx.bids, self.policy.exit_fee_pct)
        except MissingMark:
            return "missing_mark"
        except LedgerStateError as exc:
            return str(exc)
        if breakdown.equity <= 0:
            return "non_positive_equity"
        return breakdown.equity

    def _reject(self, intent: OrderIntent, reason: str, regime: RiskRegime) -> RiskDecision:
        return RiskDecision(
            allowed=False,
            reason=reason,
            client_order_id=intent.client_order_id,
            regime=regime,
        )

    def _allow(
        self,
        intent: OrderIntent,
        reason: str,
        regime: RiskRegime,
        *,
        reservation_id: str | None = None,
        reserved_quote: Decimal | None = None,
    ) -> RiskDecision:
        return RiskDecision(
            allowed=True,
            reason=reason,
            client_order_id=intent.client_order_id,
            reservation_id=reservation_id,
            reserved_quote=reserved_quote,
            regime=regime,
        )
