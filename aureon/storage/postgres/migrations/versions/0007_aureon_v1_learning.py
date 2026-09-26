"""0007_aureon_v1_learning -- canonical memory and champion/challenger lifecycle.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canonical_training_examples",
        sa.Column("example_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("market_date", sa.String(), nullable=False),
        sa.Column("setup_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("timeframe", sa.String(), nullable=False),
        sa.Column("feature_schema", sa.String(), nullable=False),
        sa.Column("label_schema", sa.String(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "setup_id",
            "feature_schema",
            "label_schema",
            name="uq_canonical_training_contract",
        ),
    )
    op.create_index(
        "ix_canonical_training_symbol_date",
        "canonical_training_examples",
        ["symbol", "market_date"],
    )

    op.add_column("model_registry", sa.Column("parent_model_id", sa.String(), nullable=True))
    op.add_column(
        "model_registry",
        sa.Column(
            "hyperparameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "validation_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "shadow_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("model_registry", sa.Column("promotion_reason", sa.String(), nullable=True))

    op.create_table(
        "model_evolution_log",
        sa.Column("decision_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("champion_model_id", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_model_evolution_symbol_time",
        "model_evolution_log",
        ["symbol", "created_at"],
    )
    op.create_index(
        "ix_model_evolution_model_time",
        "model_evolution_log",
        ["model_id", "created_at"],
    )


def downgrade() -> None:
    raise RuntimeError("no downgrade: V1 learning/evolution history is intentionally preserved")
