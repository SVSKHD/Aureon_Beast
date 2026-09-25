"""0004_eod_training_memory -- store daily setup training examples and status.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_examples",
        sa.Column("example_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("market_date", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("timeframe", sa.String(), nullable=False),
        sa.Column("setup_id", sa.String(), nullable=False),
        sa.Column("family", sa.String(), nullable=False),
        sa.Column("direction_context", sa.String(), nullable=False),
        sa.Column("setup_version", sa.String(), nullable=False),
        sa.Column("feature_schema_version", sa.String(), nullable=False),
        sa.Column("label_schema_version", sa.String(), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("agent_read", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("six_dollar_status", sa.String(), nullable=False),
        sa.Column("six_dollar_reached", sa.Boolean(), nullable=True),
        sa.Column("six_dollar_reference_price", sa.Float(), nullable=True),
        sa.Column("six_dollar_threshold_price", sa.Float(), nullable=True),
        sa.Column("six_dollar_reached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_to_six_seconds", sa.Float(), nullable=True),
        sa.Column("mfe_points", sa.Float(), nullable=True),
        sa.Column("mae_points", sa.Float(), nullable=True),
        sa.Column("mae_before_six_price", sa.Float(), nullable=True),
        sa.Column("evaluation_rule_id", sa.String(), nullable=True),
        sa.Column("evaluation_complete", sa.Boolean(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "setup_id",
            "feature_schema_version",
            "label_schema_version",
            name="uq_training_example_contract",
        ),
    )
    op.create_index(
        "ix_training_examples_symbol_date",
        "training_examples",
        ["symbol", "market_date"],
    )
    op.create_index(
        "ix_training_examples_timeframe",
        "training_examples",
        ["symbol", "timeframe", "market_date"],
    )

    op.create_table(
        "daily_training_status",
        sa.Column("status_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("market_date", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("feature_schema_version", sa.String(), nullable=False),
        sa.Column("label_schema_version", sa.String(), nullable=False),
        sa.Column("examples_written", sa.Integer(), nullable=False),
        sa.Column("reached_six", sa.Integer(), nullable=False),
        sa.Column("pending_six", sa.Integer(), nullable=False),
        sa.Column("unavailable_six", sa.Integer(), nullable=False),
        sa.Column("complete_evaluations", sa.Integer(), nullable=False),
        sa.Column("mae_before_six_available", sa.Integer(), nullable=False),
        sa.Column("by_timeframe", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_daily_training_status_symbol_date",
        "daily_training_status",
        ["symbol", "market_date"],
        unique=True,
    )


def downgrade() -> None:
    raise RuntimeError("no downgrade: 0004 training memory is intentionally preserved")
