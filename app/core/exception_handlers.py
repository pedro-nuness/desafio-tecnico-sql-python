"""Global HTTP error responses. Failed executions are already recorded by the graph.

The error class gives the status (who is at fault); message and payload are the body.
Anything that is not an AppError is a bug: logged with its traceback, answered as a
generic 500 so internals never leak.
"""

import logging
import math

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import JsonValue

from app.shared.errors import AppError, DomainError, NotFoundError
from app.shared.integrations.errors import IntegrationError

logger = logging.getLogger(__name__)


def _status(exc: AppError) -> int:
    match exc:
        case NotFoundError():
            return status.HTTP_404_NOT_FOUND
        case DomainError():
            return status.HTTP_400_BAD_REQUEST
        case IntegrationError() if exc.retry_after is not None:
            return status.HTTP_503_SERVICE_UNAVAILABLE
        case IntegrationError() if exc.timeout:
            return status.HTTP_504_GATEWAY_TIMEOUT
        case IntegrationError():
            return status.HTTP_502_BAD_GATEWAY
        case _:
            return status.HTTP_500_INTERNAL_SERVER_ERROR


def _response(
    request: Request, exc: Exception, status_code: int, content: dict[str, JsonValue]
) -> JSONResponse:
    logger.log(
        logging.WARNING if status_code < 500 else logging.ERROR,
        "Request %s %s failed: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    progress = getattr(request.state, "modernization_progress", None)
    if progress is not None and progress.execution_id is not None:
        content["execution_id"] = str(progress.execution_id)
    return JSONResponse(status_code=status_code, content=content)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        response = _response(request, exc, _status(exc), {"detail": exc.message, **exc.payload})
        if isinstance(exc, IntegrationError) and exc.retry_after is not None:
            response.headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after)))
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return _response(
            request,
            exc,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"detail": "Internal Server Error"},
        )
