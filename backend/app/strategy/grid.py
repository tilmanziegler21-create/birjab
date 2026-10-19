"""Dip grid. The strategy emits intents; the risk manager and paper matcher decide execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping

from app.accounting.fees import exit_limit_price, meets_net_target, round_trip_net_pct
from app.accounting.ledger import Ledger
from app.accounting.lots import Lot
from app.execution.orders import PaperOrder
from app.market.orderbook import OrderBook
from app.money import money
from app.risk.manager import MarketContext
from app.schemas.orders import OrderIntent, OrderState
from app.schemas.risk import RiskRegime

StrategyId = Literal["baseline_grid", "ai_filtered_grid"]
_WORKING = {
    OrderState.PREPARED,
    OrderState.SUBMITTED,
    OrderState.ACKNOWLEDGED,
    OrderState.OPEN,
    OrderState.PARTIAL,
    OrderState.UNKNOWN,
}


@dataclass(frozen=True)
class GridParams:
    strategy_id: StrategyId
    per_order_quote: Decimal
    grid_step_pct: Decimal
    net_target_pct: Decimal
    maker_fee_pct: Decimal
    taker_fee_pct: Decimal
    slippage_pct: Decimal
    min_depth_multiplier: Decimal = Decimal("10")
    quote_currency: str = "USDC"


@dataclass(frozen=True)
class GridProposal:
    intents: list[OrderIntent]
    skipped: list[str]


class GridStrategy:
    def __init__(self, params: GridParams) -> None:
        self.params = params
        self.anchors: dict[str, Decimal] = {}

    def propose(
        self,
        *,
        book: OrderBook,
        ledger: Ledger,
        orders: Mapping[str, PaperOrder],
    ) -> GridProposal:
        self._follow_fills(ledger)
        intents = self._exit_intents(book, ledger, orders)
        expected = round_trip_net_pct(
            step_pct=self.params.grid_step_pct,
            entry_fee_pct=self.params.maker_fee_pct,
            exit_fee_pct=self.params.taker_fee_pct,
            slippage_pct=self.params.slippage_pct,
        )
        if not meets_net_target(expected_net_pct=expected, net_target_pct=self.params.net_target_pct):
            return GridProposal(intents=intents, skipped=["below_net_target"])
        if _working_buy(orders, book.symbol):
            return GridProposal(intents=intents, skipped=[])
        required = self.params.per_order_quote * self.params.min_depth_multiplier
        if book.ask_depth_quote() < required:
            return GridProposal(intents=intents, skipped=["insufficient_depth"])
        if book.symbol not in self.anchors:
            self.anchors[book.symbol] = book.best_ask()
        level = sum(1 for lot in ledger.lots.values() if lot.symbol == book.symbol) + 1
        limit = self.anchors[book.symbol] * (Decimal("1") - self.params.grid_step_pct / Decimal("100"))
        base = book.symbol.split("/")[0]
        client_order_id = f"{self.params.strategy_id}-{base}-buy-{level}"
        # A canceled entry (HALT, restart after HALT) must not be re-proposed under its spent id:
        # the adapter and the ledger would both answer "idempotent" and the pair would never re-enter.
        retry = 0
        while _spent_entry(client_order_id, orders, ledger):
            retry += 1
            client_order_id = f"{self.params.strategy_id}-{base}-buy-{level}-r{retry}"
        intents.append(
            OrderIntent(
                client_order_id=client_order_id,
                symbol=book.symbol,
                side="buy",
                purpose="entry",
                quote_value=self.params.per_order_quote,
                limit_price=limit,
                reason="grid dip entry",
                strategy_id=self.params.strategy_id,
            )
        )
        return GridProposal(intents=intents, skipped=[])

    def sync(self, adapter: object, book: OrderBook, ctx: MarketContext) -> GridProposal:
        from app.adapters.paper import PaperAdapter

        if not isinstance(adapter, PaperAdapter) or adapter.ledger is None:
            raise RuntimeError("grid sync requires a configured paper adapter")
        adapter.update_book(book)
        first = self.propose(book=book, ledger=adapter.ledger, orders=adapter.orders)
        for intent in first.intents:
            adapter.place_order(intent, ctx)
        adapter.match_resting()
        second = self.propose(book=book, ledger=adapter.ledger, orders=adapter.orders)
        for intent in second.intents:
            adapter.place_order(intent, ctx)
        adapter.match_resting()
        return GridProposal(
            intents=[*first.intents, *second.intents],
            skipped=[*first.skipped, *second.skipped],
        )

    def _follow_fills(self, ledger: Ledger) -> None:
        for lot in ledger.lots.values():
            current = self.anchors.get(lot.symbol)
            if current is None or lot.entry_price < current:
                self.anchors[lot.symbol] = lot.entry_price

    def _exit_intents(
        self,
        book: OrderBook,
        ledger: Ledger,
        orders: Mapping[str, PaperOrder],
    ) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        for lot in ledger.lots.values():
            if lot.symbol != book.symbol:
                continue
            if _working_sell(orders, lot.lot_id):
                continue
            hold = ledger.holds.get(f"{self.params.strategy_id}-{lot.lot_id}-sell")
            if hold is not None and hold.status == "active" and hold.quantity > 0:
                # Restart: the ledger still holds base for this exit. Re-propose the same id so the
                # adapter rebuilds the resting order instead of leaving the base stuck.
                intents.append(self._exit_intent(lot, quantity=hold.quantity))
                continue
            if lot.available_base_qty <= 0:
                continue
            intents.append(self._exit_intent(lot))
        return intents

    def _exit_intent(self, lot: Lot, quantity: Decimal | None = None) -> OrderIntent:
        notional = lot.entry_price * lot.gross_base_qty
        if lot.entry_fee_currency == lot.quote_asset and notional > 0:
            entry_fee_pct = lot.entry_fee_amount / notional * Decimal("100")
        else:
            entry_fee_pct = self.params.maker_fee_pct
        limit = exit_limit_price(
            entry_price=lot.entry_price,
            entry_fee_pct=entry_fee_pct,
            exit_fee_pct=self.params.maker_fee_pct,
            slippage_pct=self.params.slippage_pct,
            net_target_pct=self.params.net_target_pct,
        )
        return OrderIntent(
            client_order_id=f"{self.params.strategy_id}-{lot.lot_id}-sell",
            symbol=lot.symbol,
            side="sell",
            purpose="exit",
            quantity=lot.available_base_qty if quantity is None else quantity,
            limit_price=limit,
            lot_id=lot.lot_id,
            reason="grid lot exit",
            strategy_id=self.params.strategy_id,
        )


class BaseGridStrategy(GridStrategy):
    def __init__(self, params: GridParams) -> None:
        super().__init__(replace(params, strategy_id="baseline_grid"))


class AIFilteredGridStrategy(GridStrategy):
    def __init__(self, params: GridParams) -> None:
        super().__init__(replace(params, strategy_id="ai_filtered_grid"))


def context_from_book(
    book: OrderBook,
    *,
    now: datetime,
    quote_deviation_pct: Decimal = Decimal("0"),
    regime: RiskRegime = RiskRegime.NORMAL,
    news_last_ok_at: datetime | None = None,
    manual_pause_buys: bool = False,
    manual_halt: bool = False,
) -> MarketContext:
    return MarketContext(
        now=now,
        feed_timestamp=book.updated_at,
        spread_pct=book.spread_pct(),
        quote_deviation_pct=money(quote_deviation_pct),
        bids={book.symbol: book.best_bid()},
        market_status_trading=True,
        news_last_ok_at=now if news_last_ok_at is None else news_last_ok_at,
        regime=regime,
        manual_pause_buys=manual_pause_buys,
        manual_halt=manual_halt,
    )


def _spent_entry(client_order_id: str, orders: Mapping[str, PaperOrder], ledger: Ledger) -> bool:
    order = orders.get(client_order_id)
    if order is not None and order.state in {OrderState.CANCELED, OrderState.REJECTED}:
        return True
    reservation = ledger.reservations.get(client_order_id)
    return reservation is not None and reservation.status != "active"


def _working_buy(orders: Mapping[str, PaperOrder], symbol: str) -> bool:
    return any(
        order.intent.symbol == symbol and order.intent.side == "buy" and order.state in _WORKING
        for order in orders.values()
    )


def _working_sell(orders: Mapping[str, PaperOrder], lot_id: str) -> bool:
    return any(
        order.intent.lot_id == lot_id and order.intent.side == "sell" and order.state in _WORKING
        for order in orders.values()
    )
