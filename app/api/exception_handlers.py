import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import ModernizationError, ModernizationNotFoundError
from app.shared.client import (
    HttpClientError,
    HttpConnectionError,
    HttpResponseError,
    HttpTimeoutError,
)

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ModernizationNotFoundError)
    async def not_found_handler(request: Request, exc: ModernizationNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc)},
        )

    @app.exception_handler(HttpTimeoutError)
    async def http_timeout_handler(request: Request, exc: HttpTimeoutError) -> JSONResponse:
        logger.warning("Upstream timeout on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"detail": "Gateway Timeout: upstream service timed out"},
        )

    @app.exception_handler(HttpConnectionError)
    async def http_connection_handler(request: Request, exc: HttpConnectionError) -> JSONResponse:
        logger.error(
            "Upstream connection error on %s %s: %s", request.method, request.url.path, exc
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Bad Gateway: failed to connect to upstream service"},
        )

    @app.exception_handler(HttpResponseError)
    async def http_response_error_handler(request: Request, exc: HttpResponseError) -> JSONResponse:
        logger.error(
            "Upstream returned error status %d on %s %s: %s",
            exc.status_code,
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": f"Bad Gateway: upstream service error ({exc.status_code})"},
        )

    @app.exception_handler(HttpClientError)
    async def http_client_handler(request: Request, exc: HttpClientError) -> JSONResponse:
        logger.error("Upstream client failure on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Bad Gateway: upstream service communication failed"},
        )

    @app.exception_handler(ModernizationError)
    async def modernization_error_handler(
        request: Request, exc: ModernizationError
    ) -> JSONResponse:
        logger.warning("Domain error on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled server exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"},
        )
