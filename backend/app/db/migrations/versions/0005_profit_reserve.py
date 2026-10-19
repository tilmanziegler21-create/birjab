"""Profit reserve column on the balance snapshot."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_profit_reserve"
down_revision = "0004_strategy_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("balance_snapshots")}
    if "profit_reserve" not in existing:
        op.add_column(
            "balance_snapshots",
            sa.Column("profit_reserve", sa.Numeric(38, 18), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("balance_snapshots", "profit_reserve")
