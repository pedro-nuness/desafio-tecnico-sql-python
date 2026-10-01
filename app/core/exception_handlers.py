"""Global HTTP error responses and failed-execution persistence."""

import asyncio
import json
import logging
import math
import subprocess

import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from openai import APIConnectionError, APIError, APITimeoutError
from openrouter.errors import NoResponseError, OpenRouterError
from pglast.parser import ParseError
from pydantic import ValidationError

from app.features.modernization.domain.exceptions import (
    GenerationError,
    ModernizationError,
    ModernizationNotFoundError,
    ValidationExecutionError,
)
from app.shared.integrations.exceptions import IntegrationError
from app.shared.resilience.circuit_breaker import CircuitOpenError

logger = logging.getLogger(__name__)


async def _error_response(
    request: Request, exc: Exception, status_code: int, detail: str
) -> JSONResponse:
    logger.log(
        logging.WARNING if status_code < 500 else logging.ERROR,
        "Request %s %s failed: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    content = {"detail": detail}
    progress = getattr(request.state, "modernization_progress", None)
    if progress is not None and progress.execution_id is not None:
        (finished,) = await asyncio.gather(
            request.app.state.container.modernization_service.record_failure(progress, exc),
            return_exceptions=True,
        )
        if isinstance(finished, BaseException):
            logger.error(
                "Could not record failure for %s",
                progress.execution_id,
                exc_info=(type(finished), finished, finished.__traceback__),
            )
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            content = {"detail": "Internal Server Error"}
        elif finished is not None:
            content["execution_id"] = str(finished.id)
    return JSONResponse(status_code=status_code, content=content)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ModernizationNotFoundError)
    async def not_found_handler(request: Request, exc: ModernizationNotFoundError) -> JSONResponse:
        return await _error_response(request, exc, status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(httpx.TimeoutException)
    @app.exception_handler(APITimeoutError)
    async def timeout_handler(request: Request, exc: Exception) -> JSONResponse:
        return await _error_response(
            request,
            exc,
            status.HTTP_504_GATEWAY_TIMEOUT,
            "Gateway Timeout: upstream service timed out",
        )

    @app.exception_handler(httpx.NetworkError)
    @app.exception_handler(APIConnectionError)
    @app.exception_handler(NoResponseError)
    async def connection_handler(request: Request, exc: Exception) -> JSONResponse:
        return await _error_response(
            request,
            exc,
            status.HTTP_502_BAD_GATEWAY,
            "Bad Gateway: failed to connect to upstream service",
        )

    @app.exception_handler(httpx.HTTPStatusError)
    @app.exception_handler(OpenRouterError)
    async def upstream_status_handler(request: Request, exc: Exception) -> JSONResponse:
        upstream_status = (
            exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code
        )
        status_code = (
            status.HTTP_504_GATEWAY_TIMEOUT
            if upstream_status in (408, 504)
            else status.HTTP_502_BAD_GATEWAY
        )
        return await _error_response(
            request,
            exc,
            status_code,
            f"Bad Gateway: upstream service error ({upstream_status})",
        )

    @app.exception_handler(CircuitOpenError)
    async def circuit_open_handler(request: Request, exc: CircuitOpenError) -> JSONResponse:
        response = await _error_response(
            request,
            exc,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Service Unavailable: upstream circuit is open",
        )
        if response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            response.headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after_seconds)))
        return response

    @app.exception_handler(IntegrationError)
    @app.exception_handler(APIError)
    @app.exception_handler(httpx.HTTPError)
    async def integration_handler(request: Request, exc: Exception) -> JSONResponse:
        return await _error_response(
            request,
            exc,
            status.HTTP_502_BAD_GATEWAY,
            "Bad Gateway: upstream service communication failed",
        )

    @app.exception_handler(json.JSONDecodeError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(SyntaxError)
    @app.exception_handler(GenerationError)
    async def generation_handler(request: Request, exc: Exception) -> JSONResponse:
        return await _error_response(
            request,
            exc,
            status.HTTP_502_BAD_GATEWAY,
            "Bad Gateway: invalid generated response",
        )

    @app.exception_handler(ParseError)
    @app.exception_handler(ModernizationError)
    async def modernization_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return await _error_response(request, exc, status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(ValidationExecutionError)
    @app.exception_handler(subprocess.TimeoutExpired)
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return await _error_response(
            request,
            exc,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Internal Server Error",
        )
