"""Generic asynchronous HTTP client for external integrations."""

import asyncio
import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

import httpx

logger = logging.getLogger(__name__)


class HttpClientError(Exception):
    """Base exception for HTTP client operations."""


class HttpConnectionError(HttpClientError):
    """Raised when connection to the remote server fails."""


class HttpTimeoutError(HttpClientError):
    """Raised when request exceeds timeout budget."""


class HttpResponseError(HttpClientError):
    """Raised when HTTP response status represents an error (4xx/5xx)."""

    def __init__(
        self, status_code: int, message: str, response: httpx.Response | None = None
    ) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.response = response


class HttpClient:
    """Reusable, asynchronous HTTP client with timeout, retry, and connection pooling."""

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        default_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.default_headers = dict(default_headers or {})
        self._custom_client = client is not None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.default_headers,
            timeout=self.timeout_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close underlying connections if client was created internally."""
        if not self._custom_client and not self._client.is_closed:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Execute HTTP request with transient retry handling and structured error wrapping."""
        req_headers = {**self.default_headers, **(headers or {})}
        timeout_budget = timeout_seconds if timeout_seconds is not None else self.timeout_seconds

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json,
                    data=data,
                    headers=req_headers,
                    timeout=timeout_budget,
                )

                if raise_for_status and response.is_error:
                    # Retry on 5xx if retry attempts remain
                    if response.is_server_error and attempt < self.max_retries:
                        await asyncio.sleep(0.2 * (2**attempt))
                        continue
                    raise HttpResponseError(
                        status_code=response.status_code,
                        message=response.text,
                        response=response,
                    )

                return response

            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                raise HttpTimeoutError(f"Request {method} {url} timed out: {exc}") from exc

            except httpx.NetworkError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                raise HttpConnectionError(f"Network error connecting to {url}: {exc}") from exc

            except httpx.HTTPError as exc:
                raise HttpClientError(f"HTTP request failed: {exc}") from exc

        raise HttpClientError(f"Request failed after {self.max_retries} retries")

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            raise_for_status=raise_for_status,
        )

    async def post(
        self,
        url: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        return await self.request(
            "POST",
            url,
            json=json,
            data=data,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            raise_for_status=raise_for_status,
        )

    async def put(
        self,
        url: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        return await self.request(
            "PUT",
            url,
            json=json,
            data=data,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            raise_for_status=raise_for_status,
        )

    async def patch(
        self,
        url: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        return await self.request(
            "PATCH",
            url,
            json=json,
            data=data,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            raise_for_status=raise_for_status,
        )

    async def delete(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        return await self.request(
            "DELETE",
            url,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            raise_for_status=raise_for_status,
        )
