"""Shared HTTP connection pools with bounded retries and safe self-healing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx

from .config import bounded_env_float, bounded_env_int, runtime_setting

_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_SAFE_RECOVERY_METHODS = {"GET", "HEAD"}
_RECOVERY_RETRY_DELAY = 0.1
_DEFAULT_TOTAL_TIMEOUT = bounded_env_float("HTTP_TOTAL_TIMEOUT", 45.0, 5.0, 120.0)
_MAX_RESPONSE_BYTES = bounded_env_int(
    "HTTP_MAX_RESPONSE_BYTES",
    2_000_000,
    65_536,
    10_000_000,
)

logger = logging.getLogger("taffy.services.http_client")

_R = TypeVar("_R")


def _parse_json_object(body: bytearray) -> dict[str, Any]:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("upstream JSON root must be an object")
    return payload


def _parse_text_body(body: bytearray) -> str:
    text = body.decode("utf-8")
    if not text.strip():
        raise ValueError("upstream text response is empty")
    return text


_PROXY_PROBE_TIMEOUT = 1.0
_PROXY_RECHECK_SECONDS = 30.0

_PROXY_ENV_NAMES = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
)


def env_proxy_url() -> str | None:
    """Return the first proxy URL configured for outbound traffic, if any.

    Process environment wins, then the immutable .env layers via
    ``runtime_setting`` so a launcher without exported proxy variables
    still honours HTTPS_PROXY from the project configuration.
    """

    for name in _PROXY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value.strip()
    for name in _PROXY_ENV_NAMES:
        value = runtime_setting(name)
        if value:
            return str(value).strip()
    return None


class _ProxyState:
    __slots__ = ("proxy", "checked_at")

    def __init__(self) -> None:
        self.proxy: str | None = None
        self.checked_at: float = 0.0


_proxy_state = _ProxyState()
_proxy_lock = asyncio.Lock()


async def _probe_proxy() -> str | None:
    """Return the configured proxy URL when its port accepts TCP, else None."""

    url_text = env_proxy_url()
    if not url_text:
        return None
    try:
        url = httpx.URL(url_text)
        host = url.host
        if not host:
            return None
        port = url.port or (443 if url.scheme == "https" else 80)
    except Exception:
        return None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=_PROXY_PROBE_TIMEOUT,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return url_text
    except Exception:
        return None


async def effective_proxy_url(*, force: bool = False) -> str | None:
    """Return the env proxy URL while the proxy is reachable, else None (direct)."""

    if force:
        async with _proxy_lock:
            proxy = await _probe_proxy()
            _proxy_state.proxy = proxy
            _proxy_state.checked_at = time.monotonic()
            return proxy
    if time.monotonic() - _proxy_state.checked_at > _PROXY_RECHECK_SECONDS:
        async with _proxy_lock:
            if time.monotonic() - _proxy_state.checked_at > _PROXY_RECHECK_SECONDS:
                proxy = await _probe_proxy()
                _proxy_state.proxy = proxy
                _proxy_state.checked_at = time.monotonic()
    return _proxy_state.proxy


def effective_proxy_sync() -> str | None:
    """Sync read of the last probed proxy decision (stale after the TTL)."""

    if time.monotonic() - _proxy_state.checked_at > _PROXY_RECHECK_SECONDS:
        return None
    return _proxy_state.proxy


def retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return a bounded Retry-After delay or exponential jitter."""

    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(15.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    target = parsedate_to_datetime(retry_after)
                    if target.tzinfo is None:
                        target = target.replace(tzinfo=timezone.utc)
                    seconds = (target - datetime.now(timezone.utc)).total_seconds()
                    return min(15.0, max(0.0, seconds))
                except (TypeError, ValueError, OverflowError):
                    pass
    return min(8.0, (2**attempt) + random.random())


def _transport_rebuild_reason(exc: BaseException) -> str | None:
    """Classify failures that can leave a long-lived connection pool stale."""

    if isinstance(exc, httpx.PoolTimeout):
        return None
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    if isinstance(exc, httpx.ReadError):
        return "read_error"
    if isinstance(exc, httpx.WriteError):
        return "write_error"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "remote_protocol"
    if isinstance(exc, httpx.ProxyError):
        return "proxy_error"
    if isinstance(exc, httpx.TimeoutException):
        return "transport_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "network_error"
    return None


@dataclass(slots=True, eq=False)
class _PoolGeneration:
    number: int
    client: httpx.AsyncClient
    active_requests: int = 0
    retired: bool = False


@dataclass(frozen=True, slots=True)
class HTTPPoolSnapshot:
    """Privacy-safe, process-local connection pool diagnostics."""

    generation: int
    rebuilds: int
    retired_clients: int
    last_rebuild_at: datetime | None
    last_reason: str | None


ClientFactory = Callable[[], httpx.AsyncClient]


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        follow_redirects=False,
        headers={"User-Agent": "taffy-qq-bot/0.2"},
        trust_env=False,
        proxy=effective_proxy_sync(),
    )


class SharedHTTPClient:
    """Own generation-managed application pools and expose bounded requests."""

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory
        self._current: _PoolGeneration | None = None
        self._retired: dict[int, _PoolGeneration] = {}
        self._client_lock = asyncio.Lock()
        self._generation = 0
        self._rebuilds = 0
        self._last_rebuild_at: datetime | None = None
        self._last_reason: str | None = None

    def _new_generation_locked(self) -> _PoolGeneration:
        self._generation += 1
        generation = _PoolGeneration(self._generation, self._client_factory())
        self._current = generation
        return generation

    async def _acquire_generation(self) -> _PoolGeneration:
        async with self._client_lock:
            generation = self._current
            if generation is None or generation.client.is_closed or generation.retired:
                await effective_proxy_url()
                generation = self._new_generation_locked()
            generation.active_requests += 1
            return generation

    async def _retire_generation(self, generation: _PoolGeneration, reason: str) -> bool:
        close_client: httpx.AsyncClient | None = None
        rebuilt = False
        async with self._client_lock:
            if generation.retired:
                return False
            generation.retired = True
            self._retired[generation.number] = generation
            if self._current is generation:
                self._current = None
                self._rebuilds += 1
                self._last_rebuild_at = datetime.now(timezone.utc)
                self._last_reason = reason
                rebuilt = True
            if generation.active_requests == 0:
                self._retired.pop(generation.number, None)
                close_client = generation.client
        if close_client is not None and not close_client.is_closed:
            await close_client.aclose()
        if rebuilt:
            logger.warning(
                "HTTP connection pool rebuilt generation=%d reason=%s",
                generation.number,
                reason,
            )
        return rebuilt

    async def _release_generation(self, generation: _PoolGeneration) -> None:
        close_client: httpx.AsyncClient | None = None
        async with self._client_lock:
            generation.active_requests = max(0, generation.active_requests - 1)
            if generation.retired and generation.active_requests == 0:
                self._retired.pop(generation.number, None)
                close_client = generation.client
        if close_client is not None and not close_client.is_closed:
            await close_client.aclose()

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[httpx.AsyncClient]:
        """Lease one pool generation and retire it after connection-state failures."""

        generation = await self._acquire_generation()
        try:
            yield generation.client
        except httpx.TransportError as exc:
            reason = _transport_rebuild_reason(exc)
            if reason is not None:
                await self._retire_generation(generation, reason)
                if env_proxy_url():
                    await effective_proxy_url(force=True)
            raise
        except asyncio.CancelledError:
            # Shielding keeps the retire running detached even if this task is
            # cancelled a second time while the lock/close path is in flight.
            await asyncio.shield(self._retire_generation(generation, "request_cancelled"))
            raise
        finally:
            await asyncio.shield(self._release_generation(generation))

    async def snapshot(self) -> HTTPPoolSnapshot:
        async with self._client_lock:
            return HTTPPoolSnapshot(
                generation=self._current.number if self._current is not None else self._generation,
                rebuilds=self._rebuilds,
                retired_clients=len(self._retired),
                last_rebuild_at=self._last_rebuild_at,
                last_reason=self._last_reason,
            )

    async def close(self) -> None:
        async with self._client_lock:
            generations = list(self._retired.values())
            if self._current is not None:
                generations.append(self._current)
            self._current = None
            self._retired.clear()
            for generation in generations:
                generation.retired = True
        clients = {
            generation.client for generation in generations if not generation.client.is_closed
        }
        if clients:
            await asyncio.gather(*(client.aclose() for client in clients))

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        attempts: int = 3,
        timeout: float | httpx.Timeout | None = None,
        total_timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Request an object-shaped JSON response within one total deadline."""

        bounded_attempts = max(1, min(int(attempts), 5))
        deadline = _DEFAULT_TOTAL_TIMEOUT if total_timeout is None else float(total_timeout)
        if not 0 < deadline <= 120:
            raise ValueError("total_timeout must be between 0 and 120 seconds")

        try:
            async with asyncio.timeout(deadline):
                return await self._request_with_retries(
                    method,
                    url,
                    attempts=bounded_attempts,
                    timeout=timeout,
                    kwargs=kwargs,
                    max_bytes=_MAX_RESPONSE_BYTES,
                    parse=_parse_json_object,
                )
        except TimeoutError as exc:
            raise RuntimeError("request deadline exceeded") from exc

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        attempts: int = 3,
        timeout: float | httpx.Timeout | None = None,
        total_timeout: float | None = None,
        max_bytes: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Request a non-JSON text body (for example a sitemap) within one deadline."""

        bounded_attempts = max(1, min(int(attempts), 5))
        bounded_max_bytes = (
            _MAX_RESPONSE_BYTES
            if max_bytes is None
            else max(65_536, min(int(max_bytes), 10_000_000))
        )
        deadline = _DEFAULT_TOTAL_TIMEOUT if total_timeout is None else float(total_timeout)
        if not 0 < deadline <= 120:
            raise ValueError("total_timeout must be between 0 and 120 seconds")

        try:
            async with asyncio.timeout(deadline):
                return await self._request_with_retries(
                    method,
                    url,
                    attempts=bounded_attempts,
                    timeout=timeout,
                    kwargs=kwargs,
                    max_bytes=bounded_max_bytes,
                    parse=_parse_text_body,
                )
        except TimeoutError as exc:
            raise RuntimeError("request deadline exceeded") from exc

    async def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        attempts: int,
        timeout: float | httpx.Timeout | None,
        kwargs: dict[str, Any],
        max_bytes: int,
        parse: Callable[[bytearray], _R],
    ) -> _R:
        last_error = "request failed"
        remaining_attempts = attempts
        recovery_retry_available = method.upper() in _SAFE_RECOVERY_METHODS
        backoff_attempt = 0

        while remaining_attempts > 0:
            remaining_attempts -= 1
            response: httpx.Response | None = None
            status_retry_delay: float | None = None
            try:
                async with self.lease() as client:
                    async with client.stream(method, url, timeout=timeout, **kwargs) as response:
                        if response.status_code in _TRANSIENT_STATUSES and remaining_attempts > 0:
                            last_error = f"HTTP {response.status_code}"
                            status_retry_delay = retry_delay(response, backoff_attempt)
                        else:
                            response.raise_for_status()
                            declared_size = response.headers.get("content-length")
                            if declared_size and declared_size.isdecimal():
                                if int(declared_size) > max_bytes:
                                    raise ValueError("upstream response is too large")
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=65_536):
                                body.extend(chunk)
                                if len(body) > max_bytes:
                                    raise ValueError("upstream response is too large")
                if status_retry_delay is not None:
                    await asyncio.sleep(status_retry_delay)
                    backoff_attempt += 1
                    continue
                return parse(body)
            except httpx.TransportError as exc:
                last_error = type(exc).__name__
                rebuild_reason = _transport_rebuild_reason(exc)
                if rebuild_reason is not None and recovery_retry_available:
                    recovery_retry_available = False
                    remaining_attempts += 1
                    await asyncio.sleep(_RECOVERY_RETRY_DELAY)
                    continue
                if remaining_attempts > 0:
                    await asyncio.sleep(retry_delay(response, backoff_attempt))
                    backoff_attempt += 1
                    continue
            except httpx.HTTPStatusError as exc:
                last_error = f"HTTP {exc.response.status_code}"
                break
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = type(exc).__name__
                break
        raise RuntimeError(last_error)


shared_http = SharedHTTPClient()


@asynccontextmanager
async def lease_http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with shared_http.lease() as client:
        yield client


async def request_json(
    method: str,
    url: str,
    *,
    attempts: int = 3,
    timeout: float | httpx.Timeout | None = None,
    total_timeout: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return await shared_http.request_json(
        method,
        url,
        attempts=attempts,
        timeout=timeout,
        total_timeout=total_timeout,
        **kwargs,
    )


async def http_pool_snapshot() -> HTTPPoolSnapshot:
    return await shared_http.snapshot()


async def request_text(
    method: str,
    url: str,
    *,
    attempts: int = 3,
    timeout: float | httpx.Timeout | None = None,
    total_timeout: float | None = None,
    max_bytes: int | None = None,
    **kwargs: Any,
) -> str:
    return await shared_http.request_text(
        method,
        url,
        attempts=attempts,
        timeout=timeout,
        total_timeout=total_timeout,
        max_bytes=max_bytes,
        **kwargs,
    )


async def close_http_client() -> None:
    await shared_http.close()


__all__ = [
    "HTTPPoolSnapshot",
    "SharedHTTPClient",
    "close_http_client",
    "effective_proxy_sync",
    "effective_proxy_url",
    "env_proxy_url",
    "http_pool_snapshot",
    "lease_http_client",
    "request_json",
    "request_text",
    "retry_delay",
    "shared_http",
]
