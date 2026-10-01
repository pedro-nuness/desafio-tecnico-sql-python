"""create evaluation_results

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("modernization_id", sa.Uuid(), nullable=False),
        sa.Column("procedure_name", sa.String(length=128), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("static_valid", sa.Boolean(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("cases_passed", sa.Integer(), nullable=False),
        sa.Column("cases_total", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("cases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["modernization_id"],
            ["modernization_history.id"],
            name=op.f("fk_evaluation_results_modernization_id_modernization_history"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_results")),
    )
    op.create_index(
        "ix_evaluation_results_procedure_created_at",
        "evaluation_results",
        ["procedure_name", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_results_procedure_created_at", table_name="evaluation_results")
    op.drop_table("evaluation_results")
