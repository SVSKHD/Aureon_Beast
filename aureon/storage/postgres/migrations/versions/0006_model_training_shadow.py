"""0006_model_training_shadow -- model registry, backtests and shadow predictions.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_registry",
        sa.Column("model_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("algorithm", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("feature_schema_version", sa.String(), nullable=False),
        sa.Column("label_schema_version", sa.String(), nullable=False),
        sa.Column("model_schema_version", sa.String(), nullable=False),
        sa.Column("trained_from", sa.String(), nullable=False),
        sa.Column("trained_through", sa.String(), nullable=False),
        sa.Column("training_samples", sa.Integer(), nullable=False),
        sa.Column("target_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_model_registry_symbol_created",
        "model_registry",
        ["symbol", "created_at"],
    )
    op.create_index(
        "ix_model_registry_symbol_status",
        "model_registry",
        ["symbol", "status"],
    )

    op.create_table(
        "model_training_runs",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("algorithm", sa.String(), nullable=False),
        sa.Column("feature_schema_version", sa.String(), nullable=False),
        sa.Column("label_schema_version", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("trained_from", sa.String(), nullable=True),
        sa.Column("trained_through", sa.String(), nullable=True),
        sa.Column("target_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("failure_message", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_model_training_symbol_started",
        "model_training_runs",
        ["symbol", "started_at"],
    )

    op.create_table(
        "model_backtests",
        sa.Column("backtest_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("algorithm", sa.String(), nullable=False),
        sa.Column("feature_schema_version", sa.String(), nullable=False),
        sa.Column("label_schema_version", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("start_market_date", sa.String(), nullable=True),
        sa.Column("end_market_date", sa.String(), nullable=True),
        sa.Column("folds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("aggregate_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("out_of_sample_predictions", sa.Integer(), nullable=False),
        sa.Column("failure_message", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_model_backtests_symbol_started",
        "model_backtests",
        ["symbol", "started_at"],
    )

    op.create_table(
        "model_predictions",
        sa.Column("prediction_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("setup_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("timeframe", sa.String(), nullable=False),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_schema_version", sa.String(), nullable=False),
        sa.Column("label_schema_version", sa.String(), nullable=False),
        sa.Column("probabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("feature_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actual_outcomes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("model_id", "setup_id", name="uq_model_prediction_setup"),
    )
    op.create_index(
        "ix_model_predictions_symbol_time",
        "model_predictions",
        ["symbol", "predicted_at"],
    )
    op.create_index(
        "ix_model_predictions_model",
        "model_predictions",
        ["model_id", "predicted_at"],
    )


def downgrade() -> None:
    raise RuntimeError("no downgrade: model research history is intentionally preserved")
