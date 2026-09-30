"""ORM <-> domain mapping. SQLAlchemy objects never leave the persistence package."""

from app.domain.enums import ModernizationStatus
from app.domain.models.modernization import Modernization, ModernizationReport
from app.infrastructure.persistence.models import ModernizationHistoryModel


def to_model(modernization: Modernization) -> ModernizationHistoryModel:
    model = ModernizationHistoryModel(id=modernization.id)
    apply_to_model(modernization, model)
    return model


def apply_to_model(modernization: Modernization, model: ModernizationHistoryModel) -> None:
    model.source_code = modernization.source_code
    model.schema_context = modernization.schema_context
    model.generated_code = modernization.generated_code
    model.report = modernization.report.model_dump(mode="json")
    model.status = modernization.status.value
    model.created_at = modernization.created_at
    model.updated_at = modernization.updated_at


def to_domain(model: ModernizationHistoryModel) -> Modernization:
    return Modernization(
        id=model.id,
        source_code=model.source_code,
        schema_context=model.schema_context,
        generated_code=model.generated_code,
        report=ModernizationReport.model_validate(model.report),
        status=ModernizationStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
