"""Phase 3 news columns for databases created before them."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_news_links"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("links_json", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("symbols_json", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("sources_json", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("regime", sa.String(32), nullable=False, server_default="NORMAL"),
    sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("news_events")}
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("news_events", column)


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("news_events", column.name)
