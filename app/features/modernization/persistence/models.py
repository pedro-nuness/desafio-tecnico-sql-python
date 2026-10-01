"""ORM model of a modernization run and its mapping to/from the domain aggregate.

SQLAlchemy objects never leave the persistence package.
"""

import uuid
from datetime import datetime
from typing import Self

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.features.modernization.domain.enums import ModernizationStatus
from app.features.modernization.domain.modernization import (
    Modernization,
    ModernizationReport,
)

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

    @classmethod
    def from_domain(cls, modernization: Modernization) -> Self:
        model = cls(id=modernization.id)
        model.update_from(modernization)
        return model

    def update_from(self, modernization: Modernization) -> None:
        self.source_code = modernization.source_code
        self.schema_context = modernization.schema_context
        self.generated_code = modernization.generated_code
        self.report = modernization.report.model_dump(mode="json")
        self.status = modernization.status.value
        self.created_at = modernization.created_at
        self.updated_at = modernization.updated_at

    def to_domain(self) -> Modernization:
        return Modernization(
            id=self.id,
            source_code=self.source_code,
            schema_context=self.schema_context,
            generated_code=self.generated_code,
            report=ModernizationReport.model_validate(self.report),
            status=ModernizationStatus(self.status),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
