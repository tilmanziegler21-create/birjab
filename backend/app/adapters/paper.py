"""Local paper matcher. Liquidity comes from the simulated book, never from an exchange."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.accounting.ledger import Ledger
from app.execution.fills import post_buy, post_sell
from app.execution.orders import PaperOrder, advance
from app.execution.reconcile import reconcile
from app.market.orderbook import Level, OrderBook, match_buy, match_sell
from app.money import money
from app.risk.manager import MarketContext, RiskManager
from app.schemas.orders import OrderIntent, OrderState
from app.schemas.risk import RiskDecision, RiskRegime


class PaperExecutionNotImplemented(RuntimeError):
    pass


DUST_QUOTE = Decimal("0.000001")


@dataclass
class Submission:
    decision: RiskDecision
    order: PaperOrder | None


class PaperAdapter:
    name = "paper"

    def __init__(
        self,
        ledger: Ledger | None = None,
        risk: RiskManager | None = None,
        *,
        maker_fee_pct: Decimal = Decimal("0.10"),
        taker_fee_pct: Decimal = Decimal("0.10"),
        slippage_pct: Decimal = Decimal("0"),
    ) -> None:
        self.ledger = ledger
        self.risk = risk
        self.maker_fee_pct = money(maker_fee_pct)
        self.taker_fee_pct = money(taker_fee_pct)
        self.slippage_pct = money(slippage_pct)
        self.orders: dict[str, PaperOrder] = {}
        self.decisions: list[Submission] = []
        self._asks: dict[str, list[Level]] = {}
        self._bids: dict[str, list[Level]] = {}
        self._books: dict[str, OrderBook] = {}

    def update_book(self, book: OrderBook) -> None:
        self._books[book.symbol] = book
        self._asks[book.symbol] = [Level(level.price, level.base_qty) for level in book.asks]
        self._bids[book.symbol] = [Level(level.price, level.base_qty) for level in book.bids]

    def place_order(self, intent: OrderIntent, ctx: MarketContext | None = None) -> Submission:
        if self.ledger is None or self.risk is None or ctx is None:
            raise PaperExecutionNotImplemented(
                "Paper adapter has no local book session. No exchange order was sent."
            )
        existing = self.orders.get(intent.client_order_id)
        if existing is not None:
            return self._record(Submission(decision=_idempotent_decision(existing, ctx.regime), order=existing))
        if intent.limit_price is None:
            raise PaperExecutionNotImplemented("paper order requires a limit price")
        decision = self.risk.authorize(intent, ctx)
        if not decision.allowed and decision.reason != "idempotent":
            return self._record(Submission(decision=decision, order=None))
        order = self._build_order(intent, decision)
        self.orders[order.client_order_id] = order
        self._match(order)
        return self._record(Submission(decision=decision, order=order))

    def restore_order(self, intent: OrderIntent) -> PaperOrder:
        """Rebuild an order that was resting before a restart, at its persisted limit price.

        The ledger reservation / base hold must still be active: that is what was authorised
        before the restart. Nothing is matched here; the next tick's book decides.
        """

        if self.ledger is None:
            raise PaperExecutionNotImplemented("paper adapter has no ledger to restore from")
        if intent.client_order_id in self.orders:
            return self.orders[intent.client_order_id]
        if intent.limit_price is None:
            raise PaperExecutionNotImplemented("restored order requires a limit price")
        if intent.side == "buy":
            reservation = self.ledger.reservations.get(intent.client_order_id)
            if reservation is None or reservation.status != "active" or reservation.remaining <= 0:
                raise KeyError(f"no active reservation for {intent.client_order_id}")
        else:
            hold = self.ledger.holds.get(intent.client_order_id)
            if hold is None or hold.status != "active" or hold.quantity <= 0:
                raise KeyError(f"no active hold for {intent.client_order_id}")
        decision = RiskDecision(
            allowed=True,
            reason="restored",
            client_order_id=intent.client_order_id,
            reservation_id=intent.client_order_id if intent.side == "buy" else None,
        )
        order = self._build_order(intent, decision)
        if order.state == OrderState.ACKNOWLEDGED:
            advance(order, OrderState.OPEN)
        self.orders[order.client_order_id] = order
        self._record(Submission(decision=decision, order=order))
        return order

    def _record(self, submission: Submission) -> Submission:
        self.decisions.append(submission)
        return submission

    def match_resting(self) -> None:
        if self.ledger is None or self.risk is None:
            raise PaperExecutionNotImplemented(
                "Paper adapter has no local book session. No exchange order was sent."
            )
        for order in list(self.orders.values()):
            if order.state in {OrderState.OPEN, OrderState.PARTIAL}:
                self._match(order)

    def cancel_order(self, client_order_id: str) -> PaperOrder:
        if self.ledger is None:
            raise PaperExecutionNotImplemented(
                "Paper adapter has no local book session. No exchange cancel was sent."
            )
        order = self.orders.get(client_order_id)
        if order is None:
            raise KeyError(client_order_id)
        advance(order, OrderState.CANCELED)
        if order.intent.side == "buy":
            self.ledger.release(client_order_id)
        else:
            self.ledger.release_base(client_order_id)
        return order

    def reconcile_unknown(self, client_order_id: str) -> PaperOrder:
        order = self.orders[client_order_id]
        return reconcile(order)

    def _build_order(self, intent: OrderIntent, decision: RiskDecision) -> PaperOrder:
        assert self.ledger is not None
        if intent.side == "buy":
            reservation = self.ledger.reservations[intent.client_order_id]
            quote_budget = reservation.remaining
            base_budget = Decimal("0")
            filled_quote = reservation.amount - reservation.remaining
        else:
            hold = self.ledger.holds[intent.client_order_id]
            quote_budget = Decimal("0")
            base_budget = hold.original_quantity or hold.quantity
            filled_quote = Decimal("0")
        prefix = f"{intent.client_order_id}-fill-"
        # Fills posted before a restart keep their ids, so the next fill continues the sequence
        # instead of colliding with an existing (idempotent) fill id.
        earlier = sorted((fid for fid in self.ledger.fills if fid.startswith(prefix)), key=lambda fid: int(fid[len(prefix) :]))
        order = PaperOrder(
            client_order_id=intent.client_order_id,
            intent=intent,
            state=OrderState.PREPARED,
            limit_price=money(intent.limit_price or Decimal("0")),
            quote_budget=quote_budget + filled_quote if intent.side == "buy" else quote_budget,
            base_budget=base_budget,
            filled_quote=filled_quote if intent.side == "buy" else Decimal("0"),
            filled_base=base_budget - hold.quantity if intent.side == "sell" else Decimal("0"),
            fill_ids=earlier,
        )
        advance(order, OrderState.SUBMITTED)
        advance(order, OrderState.ACKNOWLEDGED)
        if intent.side == "sell" and order.filled_base > 0 and order.remaining_base > 0:
            advance(order, OrderState.PARTIAL)
        elif intent.side == "buy" and order.filled_quote > 0 and order.remaining_quote > 0:
            advance(order, OrderState.PARTIAL)
        elif (intent.side == "buy" and order.remaining_quote <= 0) or (
            intent.side == "sell" and order.remaining_base <= 0
        ):
            advance(order, OrderState.FILLED)
        return order

    def _match(self, order: PaperOrder) -> None:
        if order.state in {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}:
            return
        book = self._books.get(order.intent.symbol)
        if book is None:
            if order.state == OrderState.ACKNOWLEDGED:
                advance(order, OrderState.OPEN)
            return
        if order.intent.side == "buy":
            self._match_buy(order)
        else:
            self._match_sell(order)

    def _match_buy(self, order: PaperOrder) -> None:
        assert self.ledger is not None
        if order.remaining_quote <= 0:
            self._to_filled(order)
            return
        fee_pct = self._fee_pct(order, book_side="ask")
        fills = match_buy(
            self._asks.get(order.intent.symbol, []),
            limit_price=order.limit_price,
            quote_budget=order.remaining_quote,
            fee_pct=fee_pct,
            slippage_pct=self.slippage_pct,
            fee_currency=order.intent.symbol.split("/")[1],
        )
        if not fills and order.state == OrderState.ACKNOWLEDGED:
            advance(order, OrderState.OPEN)
            return
        for fill in fills:
            sequence = len(order.fill_ids) + 1
            fill_id = f"{order.client_order_id}-fill-{sequence}"
            lot_id = f"{order.client_order_id}-lot-{sequence}"
            post_buy(
                self.ledger,
                fill_id=fill_id,
                reservation_id=order.client_order_id,
                lot_id=lot_id,
                symbol=order.intent.symbol,
                fill=fill,
            )
            order.fill_ids.append(fill_id)
            order.filled_quote += fill.quote_amount
            order.filled_base += fill.quantity
        self._finish_buy(order)

    def _match_sell(self, order: PaperOrder) -> None:
        assert self.ledger is not None
        if order.remaining_base <= 0:
            self._to_filled(order)
            return
        fee_pct = self._fee_pct(order, book_side="bid")
        fills = match_sell(
            self._bids.get(order.intent.symbol, []),
            limit_price=order.limit_price,
            base_qty=order.remaining_base,
            fee_pct=fee_pct,
            slippage_pct=self.slippage_pct,
            fee_currency=order.intent.symbol.split("/")[1],
        )
        if not fills and order.state == OrderState.ACKNOWLEDGED:
            advance(order, OrderState.OPEN)
            return
        for fill in fills:
            sequence = len(order.fill_ids) + 1
            fill_id = f"{order.client_order_id}-fill-{sequence}"
            post_sell(
                self.ledger,
                fill_id=fill_id,
                lot_id=order.intent.lot_id or "",
                hold_id=order.client_order_id,
                fill=fill,
            )
            order.fill_ids.append(fill_id)
            order.filled_quote += fill.quote_amount
            order.filled_base += fill.quantity
        self._finish_sell(order)

    def _finish_buy(self, order: PaperOrder) -> None:
        assert self.ledger is not None
        if 0 < order.remaining_quote < DUST_QUOTE and order.filled_quote > 0:
            # Quantities are finite; the budget that cannot buy one more step returns to free cash.
            self.ledger.release(order.client_order_id)
            order.quote_budget = order.filled_quote
        if order.remaining_quote <= 0:
            self._to_filled(order)
        elif order.filled_quote > 0 and order.state != OrderState.PARTIAL:
            advance(order, OrderState.PARTIAL)
        elif order.state == OrderState.ACKNOWLEDGED:
            advance(order, OrderState.OPEN)

    def _finish_sell(self, order: PaperOrder) -> None:
        if order.remaining_base <= 0:
            self._to_filled(order)
        elif order.filled_base > 0 and order.state != OrderState.PARTIAL:
            advance(order, OrderState.PARTIAL)
        elif order.state == OrderState.ACKNOWLEDGED:
            advance(order, OrderState.OPEN)

    def _to_filled(self, order: PaperOrder) -> None:
        if order.state == OrderState.FILLED:
            return
        if order.state == OrderState.ACKNOWLEDGED:
            advance(order, OrderState.FILLED)
        elif order.state in {OrderState.OPEN, OrderState.PARTIAL}:
            advance(order, OrderState.FILLED)

    def _fee_pct(self, order: PaperOrder, *, book_side: str) -> Decimal:
        book = self._books[order.intent.symbol]
        if book_side == "ask":
            best = book.best_ask()
            crossing = order.limit_price > best
        else:
            best = book.best_bid()
            crossing = order.limit_price < best
        return self.taker_fee_pct if crossing else self.maker_fee_pct


def _idempotent_decision(order: PaperOrder, regime: RiskRegime) -> RiskDecision:
    return RiskDecision(
        allowed=True,
        reason="idempotent",
        client_order_id=order.client_order_id,
        reservation_id=order.client_order_id if order.intent.side == "buy" else None,
        regime=regime,
    )
