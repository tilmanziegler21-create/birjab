"""Grid anchors and resting orders, so a restart does not re-anchor the grid."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.models import StrategyAnchorRow, WorkingOrderRow

revision = "0004_strategy_state"
down_revision = "0003_decision_log_equity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "strategy_anchors" not in existing:
        StrategyAnchorRow.__table__.create(bind)
    if "working_orders" not in existing:
        WorkingOrderRow.__table__.create(bind)


def downgrade() -> None:
    op.drop_table("working_orders")
    op.drop_table("strategy_anchors")
