"""0003_detection_agent_evidence -- persist normalized same-candle agent facts.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "detections",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("detections", "evidence", server_default=None)


def downgrade() -> None:
    # Additive-only: dropping stored research evidence would destroy reproducibility.
    raise RuntimeError("no downgrade: 0003 is intentionally irreversible")
