"""0002_setup_agent_confluence -- persist the six-agent setup snapshot.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "setups",
        sa.Column(
            "agent_confluence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("setups", "agent_confluence", server_default=None)


def downgrade() -> None:
    # Additive-only migration. Existing setup evidence must never be discarded.
    raise RuntimeError("0002 is intentionally irreversible")
