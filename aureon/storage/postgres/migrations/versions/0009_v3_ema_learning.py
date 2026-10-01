"""0009_v3_ema_learning -- EMA journeys, canonical examples, predictions and holdouts.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "ema_movement_journeys",
        sa.Column("journey_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("account_scope", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("timeframe", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("market_date", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.Column("payload", json_type, nullable=False),
    )
    op.create_index(
        "ix_ema_journeys_stream",
        "ema_movement_journeys",
        ["symbol", "timeframe", "status", "started_at"],
    )
    op.create_index(
        "ix_ema_journeys_date",
        "ema_movement_journeys",
        ["symbol", "market_date"],
    )

    op.create_table(
        "v3_ema_examples",
        sa.Column("example_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("journey_id", sa.String(), nullable=False),
        sa.Column("detection_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("timeframe", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("market_date", sa.String(), nullable=False),
        sa.Column("anchor_type", sa.String(), nullable=False),
        sa.Column("feature_schema", sa.String(), nullable=False),
        sa.Column("label_schema", sa.String(), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "detection_id",
            "feature_schema",
            "label_schema",
            name="uq_v3_ema_example",
        ),
    )
    op.create_index(
        "ix_v3_ema_examples_symbol_date",
        "v3_ema_examples",
        ["symbol", "market_date"],
    )
    op.create_index(
        "ix_v3_ema_examples_journey",
        "v3_ema_examples",
        ["journey_id"],
    )

    op.create_table(
        "v3_ema_predictions",
        sa.Column("prediction_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("journey_id", sa.String(), nullable=False),
        sa.Column("detection_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_outcome", json_type, nullable=True),
        sa.Column("outcome_class", sa.String(), nullable=True),
        sa.UniqueConstraint("model_id", "detection_id", name="uq_v3_ema_prediction"),
    )
    op.create_index(
        "ix_v3_ema_predictions_symbol_time",
        "v3_ema_predictions",
        ["symbol", "predicted_at"],
    )

    op.create_table(
        "v3_ema_holdout_days",
        sa.Column("holdout_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("market_date", sa.String(), nullable=False),
        sa.Column("frozen_model_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", json_type, nullable=False),
    )
    op.create_index(
        "ix_v3_ema_holdout_symbol_date",
        "v3_ema_holdout_days",
        ["symbol", "market_date"],
        unique=True,
    )


def downgrade() -> None:
    raise RuntimeError("no downgrade: V3 EMA learning history is intentionally preserved")
