"""Small in-process sliding-window limiter for single-worker deployments."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("limit, window_seconds, and max_keys must be positive")
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self._window
        async with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self._limit:
                return False
            events.append(now)
            if len(self._events) > self._max_keys:
                self._evict_idle(cutoff, protected=key)
            return True

    def _evict_idle(self, cutoff: float, protected: str) -> None:
        for key in tuple(self._events):
            if key == protected:
                continue
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._events.pop(key, None)
            if len(self._events) <= self._max_keys:
                return

        # A flood of distinct active keys must not turn the limiter itself into
        # an unbounded memory sink. Evict the least recently active key while
        # preserving the request currently being decided.  The min() scan is
        # O(n) per eviction and only runs once the key cap is exceeded (a
        # pathological many-identities window), never on the steady path.
        while len(self._events) > self._max_keys:
            candidates = [key for key in self._events if key != protected]
            if not candidates:
                return
            oldest = min(candidates, key=lambda key: self._events[key][-1])
            self._events.pop(oldest, None)
