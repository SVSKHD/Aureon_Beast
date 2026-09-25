"""0005_move_ladder_training -- add $20/$40/max-move training labels.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "training_examples",
        sa.Column("twenty_dollar_reached", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "training_examples",
        sa.Column("forty_dollar_reached", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "training_examples",
        sa.Column("time_to_twenty_seconds", sa.Float(), nullable=True),
    )
    op.add_column(
        "training_examples",
        sa.Column("time_to_forty_seconds", sa.Float(), nullable=True),
    )
    op.add_column(
        "training_examples",
        sa.Column("max_favourable_move_price", sa.Float(), nullable=True),
    )
    op.add_column(
        "training_examples",
        sa.Column("extension_after_six_price", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("no downgrade: 0005 movement labels are intentionally preserved")
