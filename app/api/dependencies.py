from typing import Annotated

from fastapi import Depends, Request

from app.application.services.modernization_service import ModernizationService
from app.bootstrap import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def get_modernization_service(
    container: Annotated[Container, Depends(get_container)],
) -> ModernizationService:
    return container.modernization_service


ModernizationServiceDep = Annotated[ModernizationService, Depends(get_modernization_service)]
