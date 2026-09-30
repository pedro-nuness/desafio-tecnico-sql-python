"""create modernization_history

Revision ID: 0001
Revises:
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "modernization_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("schema_context", sa.Text(), nullable=True),
        sa.Column("generated_code", sa.Text(), nullable=True),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failure')",
            name=op.f("ck_modernization_history_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_modernization_history")),
    )
    op.create_index(
        "ix_modernization_history_status_created_at",
        "modernization_history",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_modernization_history_status_created_at", table_name="modernization_history")
    op.drop_table("modernization_history")
