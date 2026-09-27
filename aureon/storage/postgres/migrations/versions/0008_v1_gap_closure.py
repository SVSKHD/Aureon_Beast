"""0008_v1_gap_closure -- exit-management state, model generations, experiences, exams.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("management_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    for name, column in (
        ("phase", sa.String()),
        ("previous_phase", sa.String()),
        ("priority", sa.String()),
        ("current_stop", sa.Float()),
        ("previous_stop", sa.Float()),
        ("detail", sa.String()),
    ):
        op.add_column("trade_management_events", sa.Column(name, column, nullable=True))
    op.add_column("control_requests", sa.Column("stop_loss", sa.Float(), nullable=True))
    op.add_column("model_registry", sa.Column("generation", sa.Integer(), nullable=True))
    op.add_column("model_predictions", sa.Column("decision", sa.String(), nullable=True))
    op.add_column("model_predictions", sa.Column("outcome_class", sa.String(), nullable=True))
    op.create_table(
        "learning_exams",
        sa.Column("exam_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("period_from", sa.String(), nullable=False),
        sa.Column("period_to", sa.String(), nullable=False),
        sa.Column("frozen_model_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index("ix_learning_exams_symbol", "learning_exams", ["symbol", "period_from"])


def downgrade() -> None:
    raise RuntimeError("no downgrade: V1 management/learning history is intentionally preserved")
