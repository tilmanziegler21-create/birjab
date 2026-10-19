"""Single cash ledger. Reservations are idempotent and cannot overspend free quote."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Iterator, Mapping

from app.accounting.fees import FeeError, buy_economics, sell_economics
from app.accounting.lots import BaseHold, Lot, Reservation, split_symbol
from app.accounting.pnl import EquityBreakdown, MissingMark, OpenLotMark, liquid_equity
from app.money import QuoteCurrency, money, require_positive


class AccountingError(Exception):
    pass


class OutOfFunds(AccountingError):
    pass


class IdempotencyConflict(AccountingError):
    pass


class InsufficientLotQuantity(AccountingError):
    pass


class InvalidFill(AccountingError):
    pass


class LedgerHalted(AccountingError):
    pass


class LedgerStateError(AccountingError):
    pass


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    event_type: str
    payload: dict[str, str]


@dataclass
class FillRecord:
    fill_id: str
    side: str
    lot_id: str
    symbol: str
    price: Decimal
    quantity: Decimal
    fee_amount: Decimal
    fee_currency: str
    quote_amount: Decimal


class Ledger:
    def __init__(self) -> None:
        self._lock = RLock()
        self.opened = False
        self.quote_currency: QuoteCurrency | None = None
        self.starting = Decimal("0")
        self.free = Decimal("0")
        self.reserved = Decimal("0")
        self.quote_in = Decimal("0")
        self.quote_out = Decimal("0")
        self.realized_pnl = Decimal("0")
        self.profit_reserve = Decimal("0")
        self.profit_reserve_pct = Decimal("0")
        self.peak_equity = Decimal("0")
        self.trading_blocked = False
        self.block_reason: str | None = None
        self.reservations: dict[str, Reservation] = {}
        self.lots: dict[str, Lot] = {}
        self.holds: dict[str, BaseHold] = {}
        self.fills: dict[str, FillRecord] = {}
        self.fees_by_currency: dict[str, Decimal] = {}
        self.events: list[LedgerEvent] = []
        self._fill_signatures: dict[str, tuple[str, ...]] = {}

    @contextmanager
    def exclusive(self) -> Iterator[Ledger]:
        with self._lock:
            yield self

    def open_account(self, quote_currency: QuoteCurrency | str, starting_balance: Decimal) -> None:
        with self._lock:
            self._open_unlocked(QuoteCurrency(quote_currency), money(starting_balance), record=True)

    def reserve(self, reservation_id: str, symbol: str, amount: Decimal) -> Reservation:
        with self._lock:
            self._require_trading()
            reservation = self._reserve_unlocked(reservation_id, symbol, money(amount), record=True)
            self._assert_identities()
            return reservation

    def release(self, reservation_id: str) -> Reservation:
        with self._lock:
            self._require_trading()
            reservation = self._release_unlocked(reservation_id, record=True)
            self._assert_identities()
            return reservation

    def apply_buy_fill(
        self,
        *,
        fill_id: str,
        reservation_id: str,
        lot_id: str,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        fee_amount: Decimal,
        fee_currency: str,
    ) -> Lot:
        with self._lock:
            self._require_trading()
            lot = self._buy_unlocked(
                fill_id=fill_id,
                reservation_id=reservation_id,
                lot_id=lot_id,
                symbol=symbol,
                price=money(price),
                quantity=money(quantity),
                fee_amount=money(fee_amount),
                fee_currency=fee_currency,
                record=True,
            )
            self._assert_identities()
            return lot

    def hold_base(self, hold_id: str, lot_id: str, quantity: Decimal) -> BaseHold:
        with self._lock:
            self._require_trading()
            hold = self._hold_unlocked(hold_id, lot_id, money(quantity), record=True)
            self._assert_identities()
            return hold

    def release_base(self, hold_id: str) -> BaseHold:
        with self._lock:
            self._require_trading()
            hold = self._release_base_unlocked(hold_id, record=True)
            self._assert_identities()
            return hold

    def apply_sell_fill(
        self,
        *,
        fill_id: str,
        lot_id: str,
        price: Decimal,
        quantity: Decimal,
        fee_amount: Decimal,
        fee_currency: str,
        hold_id: str | None = None,
    ) -> Decimal:
        with self._lock:
            self._require_trading()
            realized = self._sell_unlocked(
                fill_id=fill_id,
                lot_id=lot_id,
                price=money(price),
                quantity=money(quantity),
                fee_amount=money(fee_amount),
                fee_currency=fee_currency,
                hold_id=hold_id,
                record=True,
            )
            self._sweep_profit_unlocked(realized, fill_id, record=True)
            self._assert_identities()
            return realized

    def mark_blocked(self, reason: str) -> None:
        with self._lock:
            self.trading_blocked = True
            self.block_reason = reason

    def clear_block(self) -> None:
        with self._lock:
            self.trading_blocked = False
            self.block_reason = None

    def equity(self, bids: Mapping[str, Decimal], exit_fee_pct: Decimal) -> EquityBreakdown:
        with self._lock:
            return self._equity_unlocked(bids, money(exit_fee_pct))

    def mark_equity(self, bids: Mapping[str, Decimal], exit_fee_pct: Decimal) -> EquityBreakdown:
        with self._lock:
            breakdown = self._equity_unlocked(bids, money(exit_fee_pct))
            if breakdown.equity > self.peak_equity:
                self.peak_equity = breakdown.equity
            return breakdown

    def active_buy_quote(self, symbol: str) -> Decimal:
        with self._lock:
            return sum(
                (
                    item.remaining
                    for item in self.reservations.values()
                    if item.status == "active" and item.side == "buy" and item.symbol == symbol
                ),
                Decimal("0"),
            )

    def portfolio_reserved_buys(self) -> Decimal:
        with self._lock:
            return sum(
                (
                    item.remaining
                    for item in self.reservations.values()
                    if item.status == "active" and item.side == "buy"
                ),
                Decimal("0"),
            )

    def open_lot_count(self, symbol: str) -> int:
        with self._lock:
            return sum(
                1
                for lot in self.lots.values()
                if lot.symbol == symbol and lot.remaining_base_qty > 0
            )

    def active_symbols(self) -> set[str]:
        with self._lock:
            symbols = {lot.symbol for lot in self.lots.values() if lot.remaining_base_qty > 0}
            symbols.update(
                item.symbol
                for item in self.reservations.values()
                if item.status == "active" and item.side == "buy"
            )
            return symbols

    def gross_position_value(self, symbol: str, bid: Decimal) -> Decimal:
        with self._lock:
            total = Decimal("0")
            for lot in self.lots.values():
                if lot.symbol == symbol and lot.remaining_base_qty > 0:
                    total += money(bid) * lot.remaining_base_qty
            return total

    def apply_event(self, event: LedgerEvent) -> None:
        """Replay one persisted event. Does not append a new event."""

        with self._lock:
            payload = event.payload
            kind = event.event_type
            if kind == "OPEN_ACCOUNT":
                self._open_unlocked(
                    QuoteCurrency(payload["quote_currency"]),
                    money(payload["starting"]),
                    record=False,
                )
            elif kind == "RESERVE":
                self._reserve_unlocked(
                    payload["reservation_id"],
                    payload["symbol"],
                    money(payload["amount"]),
                    record=False,
                )
            elif kind == "RELEASE":
                self._release_unlocked(payload["reservation_id"], record=False)
            elif kind == "BUY_FILL":
                self._buy_unlocked(
                    fill_id=payload["fill_id"],
                    reservation_id=payload["reservation_id"],
                    lot_id=payload["lot_id"],
                    symbol=payload["symbol"],
                    price=money(payload["price"]),
                    quantity=money(payload["quantity"]),
                    fee_amount=money(payload["fee_amount"]),
                    fee_currency=payload["fee_currency"],
                    record=False,
                )
            elif kind == "HOLD_BASE":
                self._hold_unlocked(
                    payload["hold_id"],
                    payload["lot_id"],
                    money(payload["quantity"]),
                    record=False,
                )
            elif kind == "RELEASE_BASE":
                self._release_base_unlocked(payload["hold_id"], record=False)
            elif kind == "SELL_FILL":
                hold_id = payload.get("hold_id") or None
                self._sell_unlocked(
                    fill_id=payload["fill_id"],
                    lot_id=payload["lot_id"],
                    price=money(payload["price"]),
                    quantity=money(payload["quantity"]),
                    fee_amount=money(payload["fee_amount"]),
                    fee_currency=payload["fee_currency"],
                    hold_id=hold_id,
                    record=False,
                )
            elif kind == "PROFIT_RESERVE":
                self._apply_sweep_unlocked(money(payload["amount"]), record=False)
            else:
                raise LedgerStateError(f"unknown event {kind}")
            self._assert_identities()

    def _require_trading(self) -> None:
        if not self.opened:
            raise LedgerStateError("account is not open")
        if self.trading_blocked:
            raise LedgerHalted(self.block_reason or "ledger_blocked")

    def _append(self, event_id: str, event_type: str, payload: dict[str, str]) -> None:
        self.events.append(LedgerEvent(event_id=event_id, event_type=event_type, payload=payload))

    def _open_unlocked(self, quote_currency: QuoteCurrency, starting: Decimal, *, record: bool) -> None:
        if self.opened:
            raise LedgerStateError("account is already open")
        if starting < 0:
            raise LedgerStateError("starting balance must be non-negative")
        self.opened = True
        self.quote_currency = quote_currency
        self.starting = starting
        self.free = starting
        self.reserved = Decimal("0")
        self.peak_equity = starting
        if record:
            self._append(
                "open-account",
                "OPEN_ACCOUNT",
                {"quote_currency": quote_currency.value, "starting": format(starting, "f")},
            )

    def _reserve_unlocked(
        self, reservation_id: str, symbol: str, amount: Decimal, *, record: bool
    ) -> Reservation:
        self._require_open()
        amount = require_positive(amount, "reservation")
        _require_symbol(symbol, self.quote_currency)
        existing = self.reservations.get(reservation_id)
        if existing is not None:
            if existing.amount != amount or existing.symbol != symbol or existing.side != "buy":
                raise IdempotencyConflict(reservation_id)
            return existing
        if self.free < amount:
            raise OutOfFunds(f"free {self.free} < {amount}")
        self.free -= amount
        self.reserved += amount
        reservation = Reservation(
            reservation_id=reservation_id,
            symbol=symbol,
            side="buy",
            amount=amount,
            remaining=amount,
            status="active",
        )
        self.reservations[reservation_id] = reservation
        if record:
            self._append(
                f"reserve:{reservation_id}",
                "RESERVE",
                {
                    "reservation_id": reservation_id,
                    "symbol": symbol,
                    "amount": format(amount, "f"),
                },
            )
        return reservation

    def _release_unlocked(self, reservation_id: str, *, record: bool) -> Reservation:
        self._require_open()
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            raise LedgerStateError(f"unknown reservation {reservation_id}")
        if reservation.status != "active":
            return reservation
        self.reserved -= reservation.remaining
        self.free += reservation.remaining
        reservation.remaining = Decimal("0")
        reservation.status = "released"
        if record:
            self._append(
                f"release:{reservation_id}",
                "RELEASE",
                {"reservation_id": reservation_id},
            )
        return reservation

    def _buy_unlocked(
        self,
        *,
        fill_id: str,
        reservation_id: str,
        lot_id: str,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        fee_amount: Decimal,
        fee_currency: str,
        record: bool,
    ) -> Lot:
        self._require_open()
        signature = (
            reservation_id,
            lot_id,
            symbol,
            format(price, "f"),
            format(quantity, "f"),
            format(fee_amount, "f"),
            fee_currency,
        )
        if fill_id in self.fills:
            if self._fill_signatures[fill_id] != signature:
                raise IdempotencyConflict(fill_id)
            return self.lots[self.fills[fill_id].lot_id]
        base, quote = _require_symbol(symbol, self.quote_currency)
        reservation = self.reservations.get(reservation_id)
        if reservation is None or reservation.side != "buy" or reservation.symbol != symbol:
            raise InvalidFill("buy fill does not match an active reservation")
        if reservation.status != "active":
            raise InvalidFill("reservation is not active")
        try:
            economics = buy_economics(
                price=price,
                quantity=quantity,
                fee_amount=fee_amount,
                fee_currency=fee_currency,
                base_asset=base,
                quote_asset=quote,
            )
        except FeeError as exc:
            raise InvalidFill(str(exc)) from exc
        if economics.quote_debit > reservation.remaining:
            raise InvalidFill("fill exceeds reserved quote")
        if lot_id in self.lots:
            raise IdempotencyConflict(lot_id)
        reservation.remaining -= economics.quote_debit
        self.reserved -= economics.quote_debit
        self.quote_out += economics.quote_debit
        if reservation.remaining == 0:
            reservation.status = "consumed"
        self._add_fee(fee_currency, fee_amount)
        lot = Lot(
            lot_id=lot_id,
            symbol=symbol,
            base_asset=base,
            quote_asset=quote,
            entry_price=price,
            gross_base_qty=quantity,
            sellable_base_qty=economics.sellable_base,
            remaining_base_qty=economics.sellable_base,
            reserved_base_qty=Decimal("0"),
            quote_cost_remaining=economics.quote_debit,
            entry_fee_amount=fee_amount,
            entry_fee_currency=fee_currency,
        )
        self.lots[lot_id] = lot
        self.fills[fill_id] = FillRecord(
            fill_id=fill_id,
            side="buy",
            lot_id=lot_id,
            symbol=symbol,
            price=price,
            quantity=quantity,
            fee_amount=fee_amount,
            fee_currency=fee_currency,
            quote_amount=economics.quote_debit,
        )
        self._fill_signatures[fill_id] = signature
        if record:
            self._append(
                f"buy:{fill_id}",
                "BUY_FILL",
                {
                    "fill_id": fill_id,
                    "reservation_id": reservation_id,
                    "lot_id": lot_id,
                    "symbol": symbol,
                    "price": format(price, "f"),
                    "quantity": format(quantity, "f"),
                    "fee_amount": format(fee_amount, "f"),
                    "fee_currency": fee_currency,
                },
            )
        return lot

    def _hold_unlocked(self, hold_id: str, lot_id: str, quantity: Decimal, *, record: bool) -> BaseHold:
        self._require_open()
        quantity = require_positive(quantity, "hold")
        existing = self.holds.get(hold_id)
        if existing is not None:
            if existing.lot_id != lot_id or existing.original_quantity != quantity:
                raise IdempotencyConflict(hold_id)
            return existing
        lot = self._lot(lot_id)
        if quantity > lot.available_base_qty:
            raise InsufficientLotQuantity(lot_id)
        lot.reserved_base_qty += quantity
        hold = BaseHold(hold_id=hold_id, lot_id=lot_id, quantity=quantity, status="active")
        self.holds[hold_id] = hold
        if record:
            self._append(
                f"hold:{hold_id}",
                "HOLD_BASE",
                {
                    "hold_id": hold_id,
                    "lot_id": lot_id,
                    "quantity": format(quantity, "f"),
                },
            )
        return hold

    def _release_base_unlocked(self, hold_id: str, *, record: bool) -> BaseHold:
        self._require_open()
        hold = self.holds.get(hold_id)
        if hold is None:
            raise LedgerStateError(f"unknown hold {hold_id}")
        if hold.status != "active":
            return hold
        lot = self._lot(hold.lot_id)
        lot.reserved_base_qty -= hold.quantity
        hold.quantity = Decimal("0")
        hold.status = "released"
        if record:
            self._append(f"release-base:{hold_id}", "RELEASE_BASE", {"hold_id": hold_id})
        return hold

    def _sell_unlocked(
        self,
        *,
        fill_id: str,
        lot_id: str,
        price: Decimal,
        quantity: Decimal,
        fee_amount: Decimal,
        fee_currency: str,
        hold_id: str | None,
        record: bool,
    ) -> Decimal:
        self._require_open()
        signature = (
            lot_id,
            format(price, "f"),
            format(quantity, "f"),
            format(fee_amount, "f"),
            fee_currency,
            hold_id or "",
        )
        if fill_id in self.fills:
            if self._fill_signatures[fill_id] != signature:
                raise IdempotencyConflict(fill_id)
            return self.fills[fill_id].quote_amount  # not pnl; caller uses stored fill on idempotent path
        lot = self._lot(lot_id)
        try:
            economics = sell_economics(
                price=price,
                quantity=quantity,
                fee_amount=fee_amount,
                fee_currency=fee_currency,
                base_asset=lot.base_asset,
                quote_asset=lot.quote_asset,
            )
        except FeeError as exc:
            raise InvalidFill(str(exc)) from exc
        if hold_id is not None:
            hold = self.holds.get(hold_id)
            if hold is None or hold.lot_id != lot_id or hold.status != "active":
                raise InvalidFill("sell fill does not match an active base hold")
            if economics.base_debit > hold.quantity:
                raise InvalidFill("sell quantity exceeds the base hold")
        elif economics.base_debit > lot.available_base_qty:
            raise InsufficientLotQuantity(lot_id)
        if economics.base_debit > lot.remaining_base_qty:
            raise InsufficientLotQuantity(lot_id)
        if economics.base_debit == lot.remaining_base_qty:
            cost = lot.quote_cost_remaining
        else:
            cost = lot.quote_cost_remaining * economics.base_debit / lot.remaining_base_qty
        realized = economics.quote_credit - cost
        if hold_id is not None:
            hold = self.holds[hold_id]
            lot.reserved_base_qty -= economics.base_debit
            hold.quantity -= economics.base_debit
            if hold.quantity == 0:
                hold.status = "consumed"
        lot.remaining_base_qty -= economics.base_debit
        lot.quote_cost_remaining -= cost
        lot.realized_pnl += realized
        self.realized_pnl += realized
        self.free += economics.quote_credit
        self.quote_in += economics.quote_credit
        self._add_fee(fee_currency, fee_amount)
        self.fills[fill_id] = FillRecord(
            fill_id=fill_id,
            side="sell",
            lot_id=lot_id,
            symbol=lot.symbol,
            price=price,
            quantity=quantity,
            fee_amount=fee_amount,
            fee_currency=fee_currency,
            quote_amount=realized,
        )
        self._fill_signatures[fill_id] = signature
        if record:
            payload = {
                "fill_id": fill_id,
                "lot_id": lot_id,
                "price": format(price, "f"),
                "quantity": format(quantity, "f"),
                "fee_amount": format(fee_amount, "f"),
                "fee_currency": fee_currency,
                "hold_id": hold_id or "",
            }
            self._append(f"sell:{fill_id}", "SELL_FILL", payload)
        return realized

    def _equity_unlocked(self, bids: Mapping[str, Decimal], exit_fee_pct: Decimal) -> EquityBreakdown:
        self._require_open()
        marks: list[OpenLotMark] = []
        for lot in self.lots.values():
            if lot.remaining_base_qty == 0:
                continue
            if lot.symbol not in bids:
                raise MissingMark(lot.symbol)
            marks.append(
                OpenLotMark(
                    symbol=lot.symbol,
                    remaining_base_qty=lot.remaining_base_qty,
                    quote_cost_remaining=lot.quote_cost_remaining,
                    bid=money(bids[lot.symbol]),
                )
            )
        return liquid_equity(
            free_quote=self.free,
            reserved_quote=self.reserved,
            profit_reserve=self.profit_reserve,
            lots=marks,
            exit_fee_pct=exit_fee_pct,
            realized_pnl=self.realized_pnl,
            fees_by_currency=dict(self.fees_by_currency),
        )

    def _add_fee(self, currency: str, amount: Decimal) -> None:
        self.fees_by_currency[currency] = self.fees_by_currency.get(currency, Decimal("0")) + amount

    def _lot(self, lot_id: str) -> Lot:
        lot = self.lots.get(lot_id)
        if lot is None:
            raise LedgerStateError(f"unknown lot {lot_id}")
        return lot

    def _require_open(self) -> None:
        if not self.opened or self.quote_currency is None:
            raise LedgerStateError("account is not open")

    def _sweep_profit_unlocked(self, realized: Decimal, fill_id: str, *, record: bool) -> Decimal:
        """Move a configured share of a profitable close out of the trading budget."""

        if realized <= 0 or self.profit_reserve_pct <= 0:
            return Decimal("0")
        amount = (realized * self.profit_reserve_pct / Decimal("100")).quantize(Decimal("0.00000001"))
        if amount <= 0:
            return Decimal("0")
        if amount > self.free:
            amount = self.free
        if amount <= 0:
            return Decimal("0")
        self._apply_sweep_unlocked(amount, record=record, fill_id=fill_id)
        return amount

    def _apply_sweep_unlocked(self, amount: Decimal, *, record: bool, fill_id: str = "") -> None:
        amount = money(amount)
        if amount <= 0:
            return
        if self.free < amount:
            raise LedgerStateError("profit reserve exceeds free cash")
        self.free -= amount
        self.profit_reserve += amount
        if record:
            self._append(
                f"profit-reserve:{fill_id or format(self.profit_reserve, 'f')}",
                "PROFIT_RESERVE",
                {"amount": format(amount, "f"), "fill_id": fill_id},
            )

    def _assert_identities(self) -> None:
        cash = self.free + self.reserved + self.profit_reserve
        expected = self.starting + self.quote_in - self.quote_out
        if cash != expected:
            raise LedgerStateError(f"cash identity broken: {cash} != {expected}")
        if self.free < 0 or self.reserved < 0 or self.profit_reserve < 0:
            raise LedgerStateError("negative balance")
        reserved_sum = sum(
            (item.remaining for item in self.reservations.values() if item.status == "active"),
            Decimal("0"),
        )
        if reserved_sum != self.reserved:
            raise LedgerStateError("reserved quote does not match active reservations")
        for lot in self.lots.values():
            if lot.remaining_base_qty < 0 or lot.reserved_base_qty < 0:
                raise LedgerStateError("negative lot quantity")
            if lot.reserved_base_qty > lot.remaining_base_qty:
                raise LedgerStateError("lot hold exceeds remaining quantity")
            if lot.quote_cost_remaining < 0:
                raise LedgerStateError("negative lot cost")


def _require_symbol(symbol: str, quote_currency: QuoteCurrency | None) -> tuple[str, str]:
    try:
        base, quote = split_symbol(symbol)
    except ValueError as exc:
        raise InvalidFill("symbol must be BASE/QUOTE") from exc
    if quote_currency is None or quote != quote_currency.value:
        raise InvalidFill("symbol quote currency does not match the account")
    if not base:
        raise InvalidFill("base asset is empty")
    return base, quote


def replay(events: list[LedgerEvent]) -> Ledger:
    ledger = Ledger()
    for event in events:
        ledger.apply_event(event)
    return ledger
