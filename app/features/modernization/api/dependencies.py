from typing import Annotated

from fastapi import Depends

from app.core.bootstrap import Container
from app.core.dependencies import get_container
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)


def get_modernization_service(
    container: Annotated[Container, Depends(get_container)],
) -> ModernizationService:
    return container.modernization_service


ModernizationServiceDep = Annotated[ModernizationService, Depends(get_modernization_service)]
