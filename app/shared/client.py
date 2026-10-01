"""Generic asynchronous HTTP client for external integrations."""

import asyncio
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

import httpx


class HttpClient:
    """Reusable, asynchronous HTTP client with timeout, retries, and connection pooling."""

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        default_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.max_retries = max_retries
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
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
        """Retry transient failures; native exceptions propagate after exhaustion."""
        for attempt in range(self.max_retries + 1):
            (response,) = await asyncio.gather(
                self._client.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json,
                    data=data,
                    headers={**self.default_headers, **(headers or {})},
                    timeout=timeout_seconds
                    if timeout_seconds is not None
                    else self.timeout_seconds,
                ),
                return_exceptions=True,
            )
            retryable = isinstance(response, (httpx.TimeoutException, httpx.NetworkError)) or (
                isinstance(response, httpx.Response)
                and raise_for_status
                and response.is_server_error
            )
            if retryable and attempt < self.max_retries:
                await asyncio.sleep(0.2 * (2**attempt))
                continue
            if isinstance(response, BaseException):
                raise response
            if raise_for_status and response.is_error:
                response.raise_for_status()
            return response

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
