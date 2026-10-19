"""Two paper arms on one tape. Every order goes through RiskManager and PaperAdapter only."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.ledger import Ledger
from app.accounting.pnl import MissingMark, drawdown_pct
from app.adapters.paper import PaperAdapter
from app.config import Settings
from app.db.repository import (
    first_equity_since,
    load,
    load_anchors,
    load_decisions,
    load_equity,
    load_working_orders,
    save,
    save_decisions,
    save_equity,
    save_strategy_state,
)
from app.db.session import make_session_factory
from app.market.orderbook import OrderBook
from app.market.public import SpotFilters
from app.market.universe import MarketFilterInput, UniversePolicy, hard_filter
from app.money import money
from app.news.events import load_news, save_news
from app.news.ingest import NewsIngestor
from app.paper.controls import Controls
from app.paper.feed import FeedError, FeedSnapshot
from app.risk.emergency import CONFIRM_LIQUIDATE, EmergencyControl
from app.risk.manager import MarketContext, RiskManager, RiskPolicy
from app.schemas.orders import OrderIntent, OrderState
from app.schemas.risk import RiskRegime
from app.strategy.grid import AIFilteredGridStrategy, BaseGridStrategy, GridParams, GridStrategy

log = logging.getLogger("ats.paper")

ARM_NAMES = ("baseline", "ai")
_WORKING = {
    OrderState.PREPARED,
    OrderState.SUBMITTED,
    OrderState.ACKNOWLEDGED,
    OrderState.OPEN,
    OrderState.PARTIAL,
    OrderState.UNKNOWN,
}
_LOG_LIMIT = 2000


@dataclass
class Arm:
    name: str
    ledger: Ledger
    risk: RiskManager
    adapter: PaperAdapter
    strategy: GridStrategy
    logged: int = 0
    saved_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    restore_checked: bool = False


@dataclass
class TickReport:
    at: datetime
    symbols_ok: list[str]
    symbols_failed: dict[str, str]
    news_reason: str
    errors: list[str] = field(default_factory=list)


class PaperRuntime:
    def __init__(
        self,
        settings: Settings,
        feed: Any,
        news: NewsIngestor,
        *,
        controls: Controls | None = None,
        engines: dict[str, Engine] | None = None,
    ) -> None:
        self.settings = settings
        self.feed = feed
        self.news = news
        self.controls = controls or Controls()
        self.engines = engines or {}
        self.quote = settings.quote_currency.value
        self.symbols = [f"{base}/{self.quote}" for base in settings.watchlist]
        self.last_bids: dict[str, Decimal] = {}
        self.last_books: dict[str, OrderBook] = {}
        self.feed_status: dict[str, dict[str, Any]] = {}
        self.strategy_skips: dict[str, dict[str, list[str]]] = {}
        self.decision_log: list[dict[str, Any]] = []
        self._unsaved_decisions: list[dict[str, Any]] = []
        self._changed_decisions: dict[tuple[str, int], dict[str, Any]] = {}  # row ids are per arm database
        self.errors: list[dict[str, str]] = []
        self.tick_count = 0
        self.restored_orders = 0
        self.last_tick_at: datetime | None = None
        self.started_at = datetime.now(timezone.utc)
        self._sessions: dict[str, sessionmaker[Session]] = {
            name: make_session_factory(engine) for name, engine in self.engines.items()
        }
        self.arms: dict[str, Arm] = {name: self._build_arm(name) for name in ARM_NAMES}
        self._load_news()
        self._load_decisions()

    # ---------------------------------------------------------------- building

    def grid_params(self, strategy_id: str) -> GridParams:
        s = self.settings
        return GridParams(
            strategy_id=strategy_id,  # type: ignore[arg-type]
            per_order_quote=s.per_order_quote,
            grid_step_pct=s.grid_step_pct,
            net_target_pct=s.net_target_pct,
            maker_fee_pct=s.assumed_maker_fee_pct,
            taker_fee_pct=s.assumed_taker_fee_pct,
            slippage_pct=s.assumed_slippage_pct,
            min_depth_multiplier=s.min_order_book_depth_multiplier,
            quote_currency=self.quote,
        )

    def risk_policy(self) -> RiskPolicy:
        s = self.settings
        return RiskPolicy(
            per_order_quote=s.per_order_quote,
            max_portfolio_exposure_pct=s.max_portfolio_exposure_pct,
            max_per_asset_exposure_pct=s.max_per_asset_exposure_pct,
            max_open_lots_per_asset=s.max_open_lots_per_asset,
            max_active_pairs=s.max_active_pairs,
            market_stale_after_sec=s.market_stale_after_sec,
            news_stale_after_sec=s.news_stale_after_sec,
            max_spread_pct=s.max_spread_pct,
            depeg_max_deviation_pct=s.depeg_max_deviation_pct,
            max_drawdown_pct=s.max_drawdown_pct,
            exit_fee_pct=s.assumed_taker_fee_pct,
        )

    def universe_policy(self) -> UniversePolicy:
        s = self.settings
        return UniversePolicy(
            quote_currency=s.quote_currency,
            watchlist=list(s.watchlist),
            min_depth_multiplier=s.min_order_book_depth_multiplier,
            max_spread_pct=s.max_spread_pct,
        )

    def _build_arm(self, name: str) -> Arm:
        ledger = self._load_ledger(name)
        if not ledger.opened:
            ledger = Ledger()
            ledger.open_account(self.quote, self.settings.starting_virtual_balance)
        ledger.profit_reserve_pct = self.settings.profit_reserve_pct
        risk = RiskManager(ledger, self.risk_policy())
        adapter = PaperAdapter(
            ledger,
            risk,
            maker_fee_pct=self.settings.assumed_maker_fee_pct,
            taker_fee_pct=self.settings.assumed_taker_fee_pct,
            slippage_pct=self.settings.assumed_slippage_pct,
        )
        strategy_id = "baseline_grid" if name == "baseline" else "ai_filtered_grid"
        kind = BaseGridStrategy if name == "baseline" else AIFilteredGridStrategy
        strategy = kind(self.grid_params(strategy_id))
        arm = Arm(name=name, ledger=ledger, risk=risk, adapter=adapter, strategy=strategy)
        factory = self._sessions.get(name)
        if factory is not None:
            with factory() as session:
                # The grid anchors as last persisted: a restart must not re-anchor at today's ask.
                strategy.anchors.update(load_anchors(session))
                arm.saved_orders = load_working_orders(session)
            self._restore_orders(arm)
        return arm

    def _restore_orders(self, arm: Arm) -> None:
        """Resting orders come back at their persisted price. A row the ledger no longer backs is an error."""

        now = _utcnow()
        for saved in arm.saved_orders.values():
            intent = OrderIntent(
                client_order_id=saved["client_order_id"],
                symbol=saved["symbol"],
                side=saved["side"],
                purpose=saved["purpose"],
                quote_value=saved["quote_budget"] if saved["side"] == "buy" else None,
                quantity=saved["base_budget"] if saved["side"] == "sell" else None,
                limit_price=saved["limit_price"],
                lot_id=saved.get("lot_id"),
                reason="restored after restart",
                strategy_id=arm.strategy.params.strategy_id,
            )
            try:
                arm.adapter.restore_order(intent)
            except (KeyError, Exception) as exc:  # noqa: BLE001 - keep the other orders
                self._error(now, f"{arm.name}:order_not_restorable:{saved['client_order_id']}:{exc}")

    def _load_ledger(self, name: str) -> Ledger:
        factory = self._sessions.get(name)
        if factory is None:
            return Ledger()
        with factory() as session:
            return load(session)

    def _load_news(self) -> None:
        factory = self._sessions.get("shared")
        if factory is None:
            return
        with factory() as session:
            store = load_news(session)
        if store.events or store.exchange_status is not None:
            self.news.store = store

    def _load_decisions(self) -> None:
        """The decision log continues across restarts; counters and A/B vetoes are not reset."""

        merged: list[dict[str, Any]] = []
        for name in ARM_NAMES:
            factory = self._sessions.get(name)
            if factory is None:
                continue
            with factory() as session:
                merged.extend(load_decisions(session, _LOG_LIMIT))
        merged.sort(key=lambda item: (item["at"], ARM_NAMES.index(item["arm"]), item["id"]))
        self.decision_log = merged[-_LOG_LIMIT:]

    def persist(self) -> None:
        at = self.last_tick_at or _utcnow()
        for name, arm in self.arms.items():
            factory = self._sessions.get(name)
            if factory is None:
                continue
            new = [item for item in self._unsaved_decisions if item["arm"] == name]
            changed = [item for item in self._changed_decisions.values() if item["arm"] == name]
            with factory() as session:
                save(session, arm.ledger)
                save_decisions(session, new, changed)
                save_strategy_state(session, dict(arm.strategy.anchors), self._working_rows(arm), at)
                mark = self._equity_mark(arm)
                if mark is not None:
                    save_equity(session, at, mark)
            self._unsaved_decisions = [item for item in self._unsaved_decisions if item["arm"] != name]
            for item in changed:
                self._changed_decisions.pop((name, item["id"]), None)
        shared = self._sessions.get("shared")
        if shared is not None:
            with shared() as session:
                save_news(session, self.news.store)

    def equity_history(self, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for name in ARM_NAMES:
            factory = self._sessions.get(name)
            if factory is None:
                out[name] = []
                continue
            with factory() as session:
                out[name] = load_equity(session, limit)
        return out

    def _working_rows(self, arm: Arm) -> list[dict[str, Any]]:
        return [
            {
                "client_order_id": order.client_order_id,
                "symbol": order.intent.symbol,
                "side": order.intent.side,
                "purpose": order.intent.purpose,
                "state": order.state.value,
                "limit_price": order.limit_price,
                "quote_budget": order.quote_budget,
                "base_budget": order.base_budget,
                "filled_quote": order.filled_quote,
                "filled_base": order.filled_base,
                "lot_id": order.intent.lot_id,
            }
            for order in arm.adapter.orders.values()
            if order.state in {OrderState.OPEN, OrderState.PARTIAL}
        ]

    def _check_restore(self, arm: Arm, at: datetime) -> None:
        """After the first tick, every order that rested before the restart must be back at its price."""

        if arm.restore_checked:
            return
        arm.restore_checked = True
        for client_order_id, saved in arm.saved_orders.items():
            rebuilt = arm.adapter.orders.get(client_order_id)
            if rebuilt is None:
                self._error(at, f"{arm.name}:order_not_restored:{client_order_id}")
                continue
            if rebuilt.limit_price != saved["limit_price"]:
                self._error(
                    at,
                    f"{arm.name}:order_restore_mismatch:{client_order_id}:saved={saved['limit_price']}:rebuilt={rebuilt.limit_price}",
                )
        self.restored_orders += len(arm.saved_orders)

    def equity_since(self, since: datetime) -> dict[str, Decimal | None]:
        """Earliest persisted equity per arm at or after `since` (for "change over the last day")."""

        out: dict[str, Decimal | None] = {}
        for name in ARM_NAMES:
            factory = self._sessions.get(name)
            if factory is None:
                out[name] = None
                continue
            with factory() as session:
                out[name] = first_equity_since(session, since)
        return out

    def _equity_mark(self, arm: Arm) -> dict[str, Any] | None:
        ledger = arm.ledger
        try:
            marked = ledger.equity(self.last_bids, self.settings.assumed_taker_fee_pct)
        except MissingMark:
            return None
        equity = marked.equity
        return {
            "equity": equity,
            "cash": marked.cash,
            "free_quote": marked.free_quote,
            "reserved_quote": marked.reserved_quote,
            "profit_reserve": marked.profit_reserve,
            "gross_position_value": marked.gross_position_value,
            "realized_pnl": marked.realized_pnl,
            "unrealized_pnl": marked.unrealized_pnl,
            "exposure_pct": (marked.gross_position_value / equity * Decimal("100")) if equity > 0 else Decimal("0"),
            "drawdown_pct": drawdown_pct(peak=ledger.peak_equity, current=equity) if ledger.peak_equity > 0 else Decimal("0"),
            "open_lots": sum(1 for lot in ledger.lots.values() if lot.remaining_base_qty > 0),
            "completed_cycles": sum(1 for lot in ledger.lots.values() if lot.status == "closed"),
        }

    # ---------------------------------------------------------------- controls

    def pause(self, now: datetime | None = None) -> None:
        self.controls.pause(now or _utcnow())

    def halt(self, now: datetime | None = None) -> None:
        self.controls.halt(now or _utcnow())

    def resume(self, now: datetime | None = None) -> None:
        self.controls.resume(now or _utcnow())

    def liquidate(self, confirmation: str, now: datetime | None = None) -> bool:
        return self.controls.request_liquidate(confirmation, now or _utcnow())

    # ---------------------------------------------------------------- tick

    def tick(self, now: datetime | None = None) -> TickReport:
        at = now or _utcnow()
        if at.tzinfo is None:
            raise ValueError("naive_timestamp")
        report = TickReport(at=at, symbols_ok=[], symbols_failed={}, news_reason="")
        try:
            report.news_reason = self.news.poll(at).reason
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad fixture
            report.news_reason = "news_error"
            self._error(at, f"news:{exc}")
        step = getattr(self.feed, "step", None)
        if callable(step):
            step()
        fetched: dict[str, tuple[FeedSnapshot, bool]] = {}
        for symbol in self.symbols:
            try:
                snap = self.feed.fetch(symbol, at)
            except FeedError as exc:
                report.symbols_failed[symbol] = str(exc)
                self.feed_status[symbol] = {
                    **self.feed_status.get(symbol, {}),
                    "ok": False,
                    "error": str(exc),
                    "checked_at": at,
                }
                continue
            allowed = self._universe_check(snap)
            fetched[symbol] = (snap, allowed)
            self.last_bids[symbol] = snap.book.best_bid()
            self.last_books[symbol] = snap.book
            report.symbols_ok.append(symbol)
        deviation = self.feed.quote_deviation_pct(at)
        for arm in self.arms.values():
            if self.controls.halt_all:
                self._cancel_resting_buys(arm, at)
            for symbol, (snap, allowed) in fetched.items():
                ctx = self._context(at, snap, allowed, deviation)
                try:
                    proposal = arm.strategy.sync(arm.adapter, snap.book, ctx)
                    self.strategy_skips.setdefault(arm.name, {})[symbol] = sorted(set(proposal.skipped))
                except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the other arm
                    self._error(at, f"{arm.name}:{symbol}:{exc}")
                if self.controls.liquidate_confirmed:
                    self._liquidate(arm, snap, ctx)
            try:
                arm.ledger.mark_equity(self.last_bids, self.settings.assumed_taker_fee_pct)
            except MissingMark as exc:
                self._error(at, f"{arm.name}:missing_mark:{exc}")
            if fetched and set(fetched) >= {o["symbol"] for o in arm.saved_orders.values()}:
                self._check_restore(arm, at)
            self._collect_decisions(arm, at)
        self.tick_count += 1
        self.last_tick_at = at
        if self.engines:
            try:
                self.persist()
            except Exception as exc:  # noqa: BLE001
                self._error(at, f"persist:{exc}")
        return report

    def _universe_check(self, snap: FeedSnapshot) -> bool:
        book = snap.book
        filters: SpotFilters = snap.filters
        base, quote = book.symbol.split("/")
        try:
            ask = book.best_ask()
            order_price = _quantize(ask, filters.tick_size)
            order_qty = _quantize(self.settings.per_order_quote / ask, filters.lot_size)
            decision = hard_filter(
                MarketFilterInput(
                    base_asset=base,
                    quote_currency=quote,  # type: ignore[arg-type]
                    market_status=filters.market_status if filters.market_status in {"trading", "halt", "break", "post_only"} else "halt",  # type: ignore[arg-type]
                    bid=book.best_bid(),
                    ask=ask,
                    order_quote=self.settings.per_order_quote,
                    order_qty=order_qty,
                    order_price=order_price,
                    ask_depth_quote=book.ask_depth_quote(),
                    bid_depth_quote=book.bid_depth_quote(),
                    min_notional=filters.min_notional,
                    tick_size=filters.tick_size,
                    lot_size=filters.lot_size,
                ),
                self.universe_policy(),
            )
            reasons = list(decision.reasons)
            allowed = decision.allowed
            spread = book.spread_pct()
        except ValueError as exc:
            reasons = [str(exc)]
            allowed = False
            spread = None
        self.feed_status[book.symbol] = {
            "ok": True,
            "error": None,
            "checked_at": snap.received_at,
            "updated_at": book.updated_at,
            "latency_ms": snap.latency_ms,
            "stale": (snap.received_at - book.updated_at).total_seconds() > self.settings.market_stale_after_sec,
            "bid": book.bids[0].price if book.bids else None,
            "ask": book.asks[0].price if book.asks else None,
            "spread_pct": spread,
            "bid_depth_quote": book.bid_depth_quote(),
            "ask_depth_quote": book.ask_depth_quote(),
            "market_status": filters.market_status,
            "allowed": allowed,
            "reasons": reasons,
        }
        return allowed

    def _context(self, at: datetime, snap: FeedSnapshot, allowed: bool, deviation: Decimal | None) -> MarketContext:
        base = snap.book.symbol.split("/")[0]
        try:
            spread = snap.book.spread_pct()
        except ValueError:
            spread = None
        return MarketContext(
            now=at,
            feed_timestamp=snap.book.updated_at,
            spread_pct=spread,
            quote_deviation_pct=None if deviation is None else money(deviation),
            bids=dict(self.last_bids),
            market_status_trading=snap.filters.market_status == "trading",
            news_last_ok_at=self.news.last_success_at,
            regime=self.news.regime_for(base, at),
            manual_pause_buys=self.controls.pause_new_buys,
            manual_halt=self.controls.halt_all,
            emergency_confirmed=False,
            pair_allowed=allowed,
        )

    def _cancel_resting_buys(self, arm: Arm, at: datetime) -> None:
        """STOP: a resting paper buy must not fill after the halt. Exits stay."""

        for order in list(arm.adapter.orders.values()):
            if order.intent.side != "buy" or order.state not in {OrderState.OPEN, OrderState.PARTIAL}:
                continue
            try:
                arm.adapter.cancel_order(order.client_order_id)
            except Exception as exc:  # noqa: BLE001
                self._error(at, f"{arm.name}:cancel:{order.client_order_id}:{exc}")
                continue
            self._append_decision(
                {
                    "at": at,
                    "arm": arm.name,
                    "client_order_id": order.client_order_id,
                    "side": "buy",
                    "symbol": order.intent.symbol,
                    "allowed": False,
                    "reason": "canceled_on_halt",
                    "regime": None,
                    "state": order.state.value,
                    "repeats": 1,
                }
            )

    def _liquidate(self, arm: Arm, snap: FeedSnapshot, ctx: MarketContext) -> None:
        symbol = snap.book.symbol
        # Resting grid exits hold the base. Release them so the emergency sell can take it at the bid.
        for order in list(arm.adapter.orders.values()):
            if order.intent.symbol != symbol or order.intent.side != "sell":
                continue
            if order.state not in {OrderState.OPEN, OrderState.PARTIAL} or order.intent.purpose == "emergency_exit":
                continue
            try:
                arm.adapter.cancel_order(order.client_order_id)
            except Exception as exc:  # noqa: BLE001
                self._error(ctx.now, f"{arm.name}:cancel_exit:{order.client_order_id}:{exc}")
        lots = [lot for lot in arm.ledger.lots.values() if lot.symbol == symbol]
        intents = EmergencyControl().request_liquidate(CONFIRM_LIQUIDATE, lots)
        if not intents:
            return
        slip = self.settings.assumed_slippage_pct / Decimal("100")
        limit = snap.book.best_bid() * (Decimal("1") - slip)
        emergency_ctx = MarketContext(
            **{**asdict_shallow(ctx), "emergency_confirmed": True, "manual_halt": True},
        )
        for intent in intents:
            priced = intent.model_copy(update={"limit_price": limit})
            try:
                arm.adapter.place_order(priced, emergency_ctx)
            except Exception as exc:  # noqa: BLE001
                self._error(ctx.now, f"{arm.name}:liquidate:{exc}")
        arm.adapter.match_resting()

    def _collect_decisions(self, arm: Arm, at: datetime) -> None:
        new = arm.adapter.decisions[arm.logged :]
        arm.logged = len(arm.adapter.decisions)
        for submission in new:
            decision = submission.decision
            if decision.reason == "idempotent":
                continue
            intent = submission.order.intent if submission.order is not None else None
            entry = {
                "at": at,
                "arm": arm.name,
                "client_order_id": decision.client_order_id,
                "side": intent.side if intent is not None else _side_from_id(decision.client_order_id or ""),
                "symbol": intent.symbol if intent is not None else _symbol_from_id(decision.client_order_id or "", self.quote),
                "allowed": decision.allowed,
                "reason": decision.reason,
                "regime": decision.regime.value,
                "state": submission.order.state.value if submission.order is not None else None,
                "repeats": 1,
            }
            if not decision.allowed and self._fold_repeat(entry):
                continue
            self._append_decision(entry)
            log.info(json.dumps(to_jsonable(entry), separators=(",", ":")))

    def _append_decision(self, entry: dict[str, Any]) -> None:
        self.decision_log.append(entry)
        self._unsaved_decisions.append(entry)
        if len(self.decision_log) > _LOG_LIMIT:
            del self.decision_log[: len(self.decision_log) - _LOG_LIMIT]

    def _fold_repeat(self, entry: dict[str, Any]) -> bool:
        """The same intent rejected for the same reason again is one line with a counter."""

        for previous in reversed(self.decision_log[-200:]):
            if previous["allowed"]:
                continue
            if (
                previous["arm"] == entry["arm"]
                and previous["client_order_id"] == entry["client_order_id"]
                and previous["reason"] == entry["reason"]
            ):
                previous["repeats"] = int(previous.get("repeats", 1)) + 1
                previous["last_at"] = entry["at"]
                if previous.get("id") is not None:
                    self._changed_decisions[(previous["arm"], previous["id"])] = previous
                return True
            if previous["client_order_id"] == entry["client_order_id"] and previous["arm"] == entry["arm"]:
                return False
        return False

    def _error(self, at: datetime, message: str) -> None:
        self.errors.append({"at": at.isoformat(), "error": message[:500]})
        if len(self.errors) > 500:
            del self.errors[:-500]
        log.warning(json.dumps({"at": at.isoformat(), "error": message[:500]}))

    # ---------------------------------------------------------------- views

    def arm_report(self, name: str) -> dict[str, Any]:
        arm = self.arms[name]
        ledger = arm.ledger
        try:
            marked = ledger.equity(self.last_bids, self.settings.assumed_taker_fee_pct)
            equity = marked.equity
            breakdown: dict[str, Any] = {
                "equity": marked.equity,
                "cash": marked.cash,
                "free_quote": marked.free_quote,
                "reserved_quote": marked.reserved_quote,
                "profit_reserve": marked.profit_reserve,
                "gross_position_value": marked.gross_position_value,
                "estimated_exit_fees": marked.estimated_exit_fees,
                "net_position_value": marked.net_position_value,
                "realized_pnl": marked.realized_pnl,
                "unrealized_pnl": marked.unrealized_pnl,
                "fees_by_currency": marked.fees_by_currency,
            }
            exposure_pct = (marked.gross_position_value / equity * Decimal("100")) if equity > 0 else Decimal("0")
            dd = drawdown_pct(peak=ledger.peak_equity, current=equity) if ledger.peak_equity > 0 else Decimal("0")
        except MissingMark as exc:
            breakdown = {
                "error": f"missing_mark:{exc}",
                "free_quote": ledger.free,
                "reserved_quote": ledger.reserved,
                "profit_reserve": ledger.profit_reserve,
            }
            exposure_pct = None
            dd = None
        decisions = [item for item in self.decision_log if item["arm"] == name]
        buys = [item for item in decisions if item["side"] == "buy"]
        rejected = [item for item in buys if not item["allowed"]]
        paused = [item for item in rejected if str(item["reason"]).startswith(("news_", "manual_"))]
        reasons: dict[str, int] = {}
        for item in rejected:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + int(item.get("repeats", 1))
        lots = [
            {
                **asdict(lot),
                "status": lot.status,
                "available_base_qty": lot.available_base_qty,
                "mark_bid": self.last_bids.get(lot.symbol),
            }
            for lot in ledger.lots.values()
        ]
        working = [
            {
                "client_order_id": order.client_order_id,
                "symbol": order.intent.symbol,
                "side": order.intent.side,
                "purpose": order.intent.purpose,
                "state": order.state.value,
                "limit_price": order.limit_price,
                "quote_budget": order.quote_budget,
                "base_budget": order.base_budget,
                "filled_quote": order.filled_quote,
                "filled_base": order.filled_base,
                "lot_id": order.intent.lot_id,
            }
            for order in arm.adapter.orders.values()
        ]
        return {
            "name": name,
            "strategy_id": arm.strategy.params.strategy_id,
            "starting": ledger.starting,
            "peak_equity": ledger.peak_equity,
            "exposure_pct": exposure_pct,
            "drawdown_pct": dd,
            "trading_blocked": ledger.trading_blocked,
            "block_reason": ledger.block_reason,
            "completed_cycles": sum(1 for lot in ledger.lots.values() if lot.status == "closed"),
            "open_lots": sum(1 for lot in ledger.lots.values() if lot.remaining_base_qty > 0),
            "active_symbols": sorted(ledger.active_symbols()),
            "entries_allowed": sum(1 for item in buys if item["allowed"]),
            "entries_rejected": len(rejected),
            "paused_entry_pct": (Decimal(len(paused)) / Decimal(len(buys)) * Decimal("100")) if buys else Decimal("0"),
            "reject_reasons": reasons,
            "strategy_skips": self.strategy_skips.get(name, {}),
            "working": working,
            "lots": lots,
            "fills": [asdict(fill) for fill in ledger.fills.values()],
            **breakdown,
        }

    def ab_summary(self) -> dict[str, Any]:
        base = self.arms["baseline"]
        ai = self.arms["ai"]
        base_equity = _safe_equity(self, base)
        ai_equity = _safe_equity(self, ai)
        vetoes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.decision_log:
            if item["arm"] != "ai" or item["allowed"] or item["side"] != "buy":
                continue
            if not str(item["reason"]).startswith("news_"):
                continue
            cid = item["client_order_id"] or ""
            if cid in seen:
                continue
            seen.add(cid)
            lot_id = cid.replace("ai_filtered_grid", "baseline_grid", 1) + "-lot-1"
            lot = base.ledger.lots.get(lot_id)
            outcome: Decimal | None = None
            if lot is not None:
                if lot.status == "closed":
                    outcome = lot.realized_pnl
                else:
                    bid = self.last_bids.get(lot.symbol)
                    if bid is not None:
                        gross = bid * lot.remaining_base_qty
                        fee = gross * self.settings.assumed_taker_fee_pct / Decimal("100")
                        outcome = gross - fee - lot.quote_cost_remaining
            vetoes.append(
                {
                    "at": item["at"],
                    "client_order_id": cid,
                    "symbol": item["symbol"],
                    "reason": item["reason"],
                    "baseline_took_it": lot is not None,
                    "baseline_lot_net": outcome,
                    "verdict": None if outcome is None else ("saved" if outcome < 0 else "missed" if outcome > 0 else "flat"),
                }
            )
        return {
            "baseline_equity": base_equity,
            "ai_equity": ai_equity,
            "equity_delta": (ai_equity - base_equity) if base_equity is not None and ai_equity is not None else None,
            "ai_vetoes": vetoes,
            "saved": sum(1 for item in vetoes if item["verdict"] == "saved"),
            "missed": sum(1 for item in vetoes if item["verdict"] == "missed"),
            "independent_events": len(self.news.store.events),
        }

    def news_view(self, now: datetime | None = None) -> dict[str, Any]:
        at = now or self.last_tick_at or _utcnow()
        events = []
        for event in sorted(self.news.store.events, key=lambda item: item.first_seen_at, reverse=True):
            events.append(
                {
                    "canonical_event_id": event.canonical_event_id,
                    "headline": event.headline,
                    "summary": event.summary,
                    "symbols": event.symbols,
                    "sources": event.sources,
                    "links": event.links,
                    "published_at": event.published_at,
                    "first_seen_at": event.first_seen_at,
                    "fetched_at": event.fetched_at,
                    "seen_count": event.seen_count,
                    "regime": event.regime.value,
                    "weight": event.weight(at, self.settings.news_stale_after_sec),
                    "illustrative": event.illustrative,
                    "price_reaction": self._price_reaction(event.symbols),
                }
            )
        regimes = {base: self.news.regime_for(base, at).value for base in self.settings.watchlist}
        status = self.news.store.exchange_status
        return {
            "last_ok_at": self.news.last_success_at,
            "analyst_calls": self.news.analyst_calls,
            "events": events,
            "regimes": regimes,
            "exchange_status": None if status is None else asdict(status),
            "source": getattr(self.news.sources[0], "name", type(self.news.sources[0]).__name__) if self.news.sources else None,
        }

    def _price_reaction(self, symbols: list[str]) -> dict[str, Decimal | None]:
        out: dict[str, Decimal | None] = {}
        for base in symbols:
            if base == "MARKET":
                continue
            out[base] = self.last_bids.get(f"{base}/{self.quote}")
        return out

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or _utcnow()
        return to_jsonable(
            {
                "generated_at": now,
                "mode": self.settings.mode,
                "live_trading": False,
                "live_adapter": "disabled",
                "quote_currency": self.quote,
                "exchange": self.settings.exchange,
                "exit_fee_pct": self.settings.assumed_taker_fee_pct,
                "feed": {
                    "name": getattr(self.feed, "name", type(self.feed).__name__),
                    "illustrative": bool(getattr(self.feed, "illustrative", False)),
                    "poll_interval_sec": self.settings.market_poll_interval_sec,
                    "symbols": self.feed_status,
                },
                "controls": {
                    "pause_new_buys": self.controls.pause_new_buys,
                    "halt_all": self.controls.halt_all,
                    "liquidate_confirmed": self.controls.liquidate_confirmed,
                    "history": self.controls.history[-20:],
                },
                "runtime": {
                    "started_at": self.started_at,
                    "tick_count": self.tick_count,
                    "last_tick_at": self.last_tick_at,
                    "errors": self.errors[-50:],
                    "persisted": bool(self.engines),
                    "restored_orders": self.restored_orders,
                    "anchors": {name: dict(arm.strategy.anchors) for name, arm in self.arms.items()},
                },
                "news": self.news_view(now),
                "equity_24h_ago": self.equity_since(now - timedelta(hours=24)),
                "arms": {name: self.arm_report(name) for name in ARM_NAMES},
                "ab": self.ab_summary(),
                "decisions": self.decision_log[-300:],
                "paused_reasons": self._paused_reasons(),
            }
        )

    def _paused_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.controls.halt_all:
            reasons.append("manual_halt")
        elif self.controls.pause_new_buys:
            reasons.append("manual_pause_buys")
        if self.controls.liquidate_confirmed:
            reasons.append("liquidate_confirmed")
        if self.news.last_success_at is None:
            reasons.append("news_unavailable_for_ai")
        for base in self.settings.watchlist:
            regime = self.news.regime_for(base, self.last_tick_at or _utcnow())
            if regime != RiskRegime.NORMAL:
                reasons.append(f"{base}:{regime.value}")
        for symbol, status in self.feed_status.items():
            if not status.get("ok"):
                reasons.append(f"{symbol}:feed:{status.get('error')}")
            elif not status.get("allowed", True):
                reasons.append(f"{symbol}:" + ",".join(status.get("reasons", [])))
        for name, arm in self.arms.items():
            if arm.ledger.trading_blocked:
                reasons.append(f"{name}:{arm.ledger.block_reason}")
        for name, per_symbol in self.strategy_skips.items():
            for symbol, skips in per_symbol.items():
                for skip in skips:
                    reasons.append(f"{name}:{symbol}:{skip}")
        return reasons


def _safe_equity(runtime: PaperRuntime, arm: Arm) -> Decimal | None:
    try:
        return arm.ledger.equity(runtime.last_bids, runtime.settings.assumed_taker_fee_pct).equity
    except MissingMark:
        return None


def asdict_shallow(ctx: MarketContext) -> dict[str, Any]:
    return {key: getattr(ctx, key) for key in ctx.__dataclass_fields__}


def _quantize(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _side_from_id(client_order_id: str) -> str:
    return "sell" if client_order_id.endswith("-sell") or client_order_id.startswith("liq-") else "buy"


def _symbol_from_id(client_order_id: str, quote: str) -> str:
    parts = client_order_id.split("-")
    if len(parts) >= 3 and parts[0] in {"baseline_grid", "ai_filtered_grid"}:
        return f"{parts[1]}/{quote}"
    return ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value
