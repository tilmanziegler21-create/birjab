"""Persistent decision log columns and the equity time series (phase 5 audit, item 3.1)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.models import EquitySnapshotRow

revision = "0003_decision_log_equity"
down_revision = "0002_news_links"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("arm", sa.String(16), nullable=False, server_default=""),
    sa.Column("side", sa.String(8), nullable=False, server_default=""),
    sa.Column("symbol", sa.String(32), nullable=False, server_default=""),
    sa.Column("state", sa.String(20), nullable=True),
    sa.Column("repeats", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("last_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("risk_decisions")}
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("risk_decisions", column)
    if "equity_snapshots" not in inspector.get_table_names():
        EquitySnapshotRow.__table__.create(bind)


def downgrade() -> None:
    op.drop_table("equity_snapshots")
    for column in reversed(_COLUMNS):
        op.drop_column("risk_decisions", column.name)
