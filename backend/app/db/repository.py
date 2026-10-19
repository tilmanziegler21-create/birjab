"""Append-only ledger events plus a balance snapshot that must match the replay."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.accounting.ledger import Ledger, LedgerEvent, replay
from app.accounting.lots import Lot
from app.db.models import (
    BalanceSnapshotRow,
    EquitySnapshotRow,
    LedgerEventRow,
    LotRow,
    RiskDecisionRow,
    StrategyAnchorRow,
    WorkingOrderRow,
)


def save(session: Session, ledger: Ledger) -> None:
    if not ledger.opened or ledger.quote_currency is None:
        raise ValueError("cannot persist an unopened ledger")
    existing = set(session.scalars(select(LedgerEventRow.event_id)).all())
    sequence = session.scalar(select(func.max(LedgerEventRow.sequence))) or 0
    now = datetime.now(timezone.utc)
    for event in ledger.events:
        if event.event_id in existing:
            continue
        sequence += 1
        session.add(
            LedgerEventRow(
                event_id=event.event_id,
                sequence=sequence,
                event_type=event.event_type,
                payload_json=json.dumps(event.payload, separators=(",", ":")),
                created_at=now,
            )
        )
    snapshot = session.get(BalanceSnapshotRow, 1)
    if snapshot is None:
        snapshot = BalanceSnapshotRow(id=1, quote_currency=ledger.quote_currency.value)
        session.add(snapshot)
    snapshot.quote_currency = ledger.quote_currency.value
    snapshot.free = ledger.free
    snapshot.reserved = ledger.reserved
    snapshot.starting = ledger.starting
    snapshot.realized_pnl = ledger.realized_pnl
    snapshot.profit_reserve = ledger.profit_reserve
    snapshot.peak_equity = ledger.peak_equity
    snapshot.trading_blocked = ledger.trading_blocked
    snapshot.block_reason = ledger.block_reason
    session.execute(delete(LotRow))
    for lot in ledger.lots.values():
        session.add(_lot_row(lot))
    session.commit()


def load(session: Session) -> Ledger:
    rows = session.scalars(select(LedgerEventRow).order_by(LedgerEventRow.sequence)).all()
    events = [
        LedgerEvent(
            event_id=row.event_id,
            event_type=row.event_type,
            payload=json.loads(row.payload_json),
        )
        for row in rows
    ]
    ledger = replay(events)
    snapshot = session.get(BalanceSnapshotRow, 1)
    if snapshot is None or not ledger.opened or ledger.quote_currency is None:
        return ledger
    matches = (
        snapshot.quote_currency == ledger.quote_currency.value
        and snapshot.free == ledger.free
        and snapshot.reserved == ledger.reserved
        and snapshot.starting == ledger.starting
        and snapshot.realized_pnl == ledger.realized_pnl
        and getattr(snapshot, "profit_reserve", Decimal("0")) == ledger.profit_reserve
    )
    if not matches:
        ledger.mark_blocked("balance_snapshot_mismatch")
        return ledger
    ledger.peak_equity = snapshot.peak_equity
    if snapshot.trading_blocked:
        ledger.mark_blocked(snapshot.block_reason or "trading_blocked")
    return ledger


def repair_from_replay(session: Session, ledger: Ledger) -> None:
    """Rewrite the snapshot from the replayed ledger. Does not accept a caller-supplied balance."""

    ledger.clear_block()
    save(session, ledger)


def save_decisions(session: Session, new: list[dict], changed: list[dict]) -> None:
    """Append new decision-log entries and refresh repeat counters. Sets entry["id"]."""

    for entry in new:
        row = RiskDecisionRow(
            client_order_id=str(entry.get("client_order_id") or ""),
            allowed=bool(entry["allowed"]),
            reason=str(entry["reason"]),
            regime=str(entry.get("regime") or ""),
            created_at=entry["at"],
            arm=str(entry["arm"]),
            side=str(entry.get("side") or ""),
            symbol=str(entry.get("symbol") or ""),
            state=entry.get("state"),
            repeats=int(entry.get("repeats", 1)),
            last_at=entry.get("last_at"),
        )
        session.add(row)
        session.flush()
        entry["id"] = row.id
    for entry in changed:
        row = session.get(RiskDecisionRow, entry.get("id"))
        if row is None:
            continue
        row.repeats = int(entry.get("repeats", 1))
        row.last_at = entry.get("last_at")
    session.commit()


def load_decisions(session: Session, limit: int) -> list[dict]:
    rows = session.scalars(select(RiskDecisionRow).order_by(RiskDecisionRow.id.desc()).limit(limit)).all()
    out: list[dict] = []
    for row in reversed(rows):
        entry: dict = {
            "id": row.id,
            "at": _aware(row.created_at),
            "arm": row.arm,
            "client_order_id": row.client_order_id,
            "side": row.side,
            "symbol": row.symbol,
            "allowed": row.allowed,
            "reason": row.reason,
            "regime": row.regime or None,
            "state": row.state,
            "repeats": row.repeats,
        }
        if row.last_at is not None:
            entry["last_at"] = _aware(row.last_at)
        out.append(entry)
    return out


def save_equity(session: Session, at: datetime, mark: dict) -> None:
    session.add(
        EquitySnapshotRow(
            at=at,
            equity=mark["equity"],
            cash=mark["cash"],
            free_quote=mark["free_quote"],
            reserved_quote=mark["reserved_quote"],
            gross_position_value=mark["gross_position_value"],
            realized_pnl=mark["realized_pnl"],
            unrealized_pnl=mark["unrealized_pnl"],
            exposure_pct=mark["exposure_pct"],
            drawdown_pct=mark["drawdown_pct"],
            open_lots=int(mark["open_lots"]),
            completed_cycles=int(mark["completed_cycles"]),
        )
    )
    session.commit()


def load_equity(session: Session, limit: int) -> list[dict]:
    rows = session.scalars(select(EquitySnapshotRow).order_by(EquitySnapshotRow.id.desc()).limit(limit)).all()
    return [
        {
            "at": _aware(row.at),
            "equity": row.equity,
            "cash": row.cash,
            "free_quote": row.free_quote,
            "reserved_quote": row.reserved_quote,
            "gross_position_value": row.gross_position_value,
            "realized_pnl": row.realized_pnl,
            "unrealized_pnl": row.unrealized_pnl,
            "exposure_pct": row.exposure_pct,
            "drawdown_pct": row.drawdown_pct,
            "open_lots": row.open_lots,
            "completed_cycles": row.completed_cycles,
        }
        for row in reversed(rows)
    ]


def save_strategy_state(session: Session, anchors: dict[str, Decimal], orders: list[dict], at: datetime) -> None:
    """Anchors and resting orders are a snapshot: replace, then commit."""

    session.execute(delete(StrategyAnchorRow))
    for symbol, anchor in anchors.items():
        session.add(StrategyAnchorRow(symbol=symbol, anchor=anchor, updated_at=at))
    session.execute(delete(WorkingOrderRow))
    for order in orders:
        session.add(
            WorkingOrderRow(
                client_order_id=order["client_order_id"],
                symbol=order["symbol"],
                side=order["side"],
                purpose=order["purpose"],
                state=order["state"],
                limit_price=order["limit_price"],
                quote_budget=order["quote_budget"],
                base_budget=order["base_budget"],
                filled_quote=order["filled_quote"],
                filled_base=order["filled_base"],
                lot_id=order.get("lot_id"),
                updated_at=at,
            )
        )
    session.commit()


def load_anchors(session: Session) -> dict[str, Decimal]:
    return {row.symbol: row.anchor for row in session.scalars(select(StrategyAnchorRow)).all()}


def load_working_orders(session: Session) -> dict[str, dict]:
    return {
        row.client_order_id: {
            "client_order_id": row.client_order_id,
            "symbol": row.symbol,
            "side": row.side,
            "purpose": row.purpose,
            "state": row.state,
            "limit_price": row.limit_price,
            "quote_budget": row.quote_budget,
            "base_budget": row.base_budget,
            "filled_quote": row.filled_quote,
            "filled_base": row.filled_base,
            "lot_id": row.lot_id,
            "updated_at": _aware(row.updated_at),
        }
        for row in session.scalars(select(WorkingOrderRow)).all()
    }


def first_equity_since(session: Session, since: datetime) -> Decimal | None:
    """Earliest recorded equity at or after `since`; None when nothing was recorded yet."""

    row = session.scalars(
        select(EquitySnapshotRow).where(EquitySnapshotRow.at >= since).order_by(EquitySnapshotRow.id.asc()).limit(1)
    ).first()
    return None if row is None else row.equity


def _aware(value: datetime) -> datetime:
    """SQLite drops tzinfo; the runtime only writes UTC."""

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _lot_row(lot: Lot) -> LotRow:
    return LotRow(
        lot_id=lot.lot_id,
        symbol=lot.symbol,
        base_asset=lot.base_asset,
        quote_asset=lot.quote_asset,
        entry_price=lot.entry_price,
        gross_base_qty=lot.gross_base_qty,
        sellable_base_qty=lot.sellable_base_qty,
        remaining_base_qty=lot.remaining_base_qty,
        reserved_base_qty=lot.reserved_base_qty,
        quote_cost_remaining=lot.quote_cost_remaining,
        entry_fee_amount=lot.entry_fee_amount,
        entry_fee_currency=lot.entry_fee_currency,
        realized_pnl=lot.realized_pnl,
        status=lot.status,
    )
