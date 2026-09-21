"""Small in-process circuit breakers and concurrency bulkheads."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable


class CircuitOpenError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    name: str
    state: str
    recent_failures: int
    retry_after_seconds: float


class CircuitBreaker:
    """Open after a bounded burst of transient failures, then allow one probe."""

    DEFAULT_PROBE_TIMEOUT_SECONDS = 300.0

    def __init__(
        self,
        name: str,
        *,
        threshold: int = 5,
        window_seconds: float = 120.0,
        open_seconds: float = 60.0,
        probe_timeout_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.threshold = max(1, int(threshold))
        self.window_seconds = max(1.0, float(window_seconds))
        self.open_seconds = max(1.0, float(open_seconds))
        self.probe_timeout_seconds = (
            self.DEFAULT_PROBE_TIMEOUT_SECONDS
            if probe_timeout_seconds is None
            else max(1.0, float(probe_timeout_seconds))
        )
        self._clock = clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._probe_active = False
        self._probe_started_at = 0.0
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            self._prune(now)
            if self._opened_at is None:
                return
            elapsed = now - self._opened_at
            if elapsed < self.open_seconds:
                raise CircuitOpenError(self.name)
            if self._probe_active:
                if now - self._probe_started_at <= self.probe_timeout_seconds:
                    raise CircuitOpenError(self.name)
                # A half-open probe whose task was cancelled can never report
                # success or failure (CancelledError bypasses ``except
                # Exception`` handlers); expire the stale slot so the breaker
                # does not stay wedged open forever.
                self._probe_active = False
            self._probe_active = True
            self._probe_started_at = now

    async def succeed(self) -> None:
        async with self._lock:
            self._failures.clear()
            self._opened_at = None
            self._probe_active = False

    async def fail(self) -> None:
        async with self._lock:
            now = self._clock()
            self._prune(now)
            self._failures.append(now)
            self._probe_active = False
            if len(self._failures) >= self.threshold:
                self._opened_at = now

    async def cancel_probe(self) -> None:
        async with self._lock:
            self._probe_active = False

    async def snapshot(self) -> CircuitSnapshot:
        async with self._lock:
            now = self._clock()
            self._prune(now)
            if self._opened_at is None:
                state = "closed"
                retry = 0.0
            else:
                retry = max(0.0, self.open_seconds - (now - self._opened_at))
                state = "open" if retry > 0 else "half_open"
            return CircuitSnapshot(self.name, state, len(self._failures), retry)


_BREAKERS: OrderedDict[str, CircuitBreaker] = OrderedDict()
_MAX_BREAKERS = 128


def get_circuit_breaker(name: str) -> CircuitBreaker:
    """Return a shared breaker, bounding the registry with LRU eviction.

    Callers must pass a small fixed vocabulary of names.  The bound is a
    defensive backstop for historical call sites that once derived names from
    request parameters; a long-lived bot must never grow this registry without
    limit.
    """

    breaker = _BREAKERS.get(name)
    if breaker is None:
        breaker = CircuitBreaker(name)
        _BREAKERS[name] = breaker
    _BREAKERS.move_to_end(name)
    while len(_BREAKERS) > _MAX_BREAKERS:
        _BREAKERS.popitem(last=False)
    return breaker


async def circuit_snapshots() -> tuple[CircuitSnapshot, ...]:
    # Snapshot the registry first: get_circuit_breaker may evict entries
    # during awaits, and iterating a mutating OrderedDict concurrently is
    # undefined behaviour.
    breakers = tuple(_BREAKERS.values())
    return tuple([await breaker.snapshot() for breaker in breakers])


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitSnapshot",
    "circuit_snapshots",
    "get_circuit_breaker",
]
