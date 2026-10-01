import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base

STATUSES = ("running", "success", "partial", "failure")


class ModernizationHistoryModel(Base):
    __tablename__ = "modernization_history"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")", name="status_valid"
        ),
        Index("ix_modernization_history_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_code: Mapped[str] = mapped_column(Text)
    schema_context: Mapped[str | None] = mapped_column(Text)
    generated_code: Mapped[str | None] = mapped_column(Text)
    report: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
