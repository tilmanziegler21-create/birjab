"""Relational schema. Money columns are exact decimals, not floats."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import MoneyAmount


class Base(DeclarativeBase):
    pass


class LedgerEventRow(Base):
    __tablename__ = "ledger_events"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BalanceSnapshotRow(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    free: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    reserved: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    starting: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    profit_reserve: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False, default=Decimal("0"))
    peak_equity: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    trading_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class LotRow(Base):
    __tablename__ = "lots"

    lot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    gross_base_qty: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    sellable_base_qty: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    remaining_base_qty: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    reserved_base_qty: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    quote_cost_remaining: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    entry_fee_amount: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    entry_fee_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class OrderRow(Base):
    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_value: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(MoneyAmount, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("fill_id", name="uq_fills_fill_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    fee_currency: Mapped[str] = mapped_column(String(8), nullable=False)


class NewsEventRow(Base):
    __tablename__ = "news_events"

    canonical_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    headline: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    links_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    symbols_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    regime: Mapped[str] = mapped_column(String(32), nullable=False, default="NORMAL")
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class RiskDecisionRow(Base):
    """One line of the runtime decision log. Repeats of the same rejection update `repeats`."""

    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arm: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    side: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    repeats: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StrategyAnchorRow(Base):
    """Grid anchor per symbol for one arm. A restart must not move the grid."""

    __tablename__ = "strategy_anchors"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    anchor: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkingOrderRow(Base):
    """Resting paper orders as last persisted; used to verify the rebuild after a restart."""

    __tablename__ = "working_orders"

    client_order_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    quote_budget: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    base_budget: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    filled_quote: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    filled_base: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    lot_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EquitySnapshotRow(Base):
    """Per-tick mark of one arm. Rows are appended; nothing is recomputed from them."""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    equity: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    cash: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    free_quote: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    reserved_quote: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    gross_position_value: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    exposure_pct: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(MoneyAmount, nullable=False)
    open_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_cycles: Mapped[int] = mapped_column(Integer, nullable=False)
