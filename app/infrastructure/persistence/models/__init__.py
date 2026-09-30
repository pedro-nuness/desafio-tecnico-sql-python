"""One ORM class per module. Import each here so Base.metadata (and Alembic) sees it."""

from app.infrastructure.persistence.models.modernization_history import ModernizationHistoryModel

__all__ = ["ModernizationHistoryModel"]
