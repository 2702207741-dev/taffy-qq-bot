"""Stale-while-revalidate semantics of the market cache (V0.4 P0).

Red lines under test: a failed answer is never cached, and a stale entry is
only served inside its grace window — beyond it the call blocks on a fresh
fetch exactly as the pre-SWR behaviour did.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from src.services.market import SteamDTService, market_cache_age

KEY = "GET:test:path"
FRESH = {"success": True, "value": "fresh"}
STALE = {"success": True, "value": "stale"}


class FakeRequest:
    """Callable returning queued responses and counting invocations."""

    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        item = self.responses.pop(0) if self.responses else FRESH
        if isinstance(item, Exception):
            raise item
        return item


def _seed(service: SteamDTService, payload: dict[str, Any], *, age: float) -> None:
    service.cache[KEY] = (time.monotonic() - age, payload)


@pytest.mark.asyncio
async def test_fresh_entry_is_served_without_any_request() -> None:
    service = SteamDTService()
    request = FakeRequest([{"success": True, "value": "never"}])
    _seed(service, FRESH, age=1.0)

    result = await service.cached_request(KEY, 30.0, request)

    assert result == FRESH
    assert request.calls == 0
    assert service.cache_age(KEY) is not None and service.cache_age(KEY) < 30.0


@pytest.mark.asyncio
async def test_expired_entry_without_grace_blocks_on_fresh_fetch() -> None:
    service = SteamDTService()
    request = FakeRequest([FRESH])
    _seed(service, STALE, age=60.0)

    result = await service.cached_request(KEY, 30.0, request)

    assert result == FRESH
    assert request.calls == 1


@pytest.mark.asyncio
async def test_stale_within_grace_is_served_and_refreshed_in_background() -> None:
    service = SteamDTService()
    request = FakeRequest([FRESH])
    _seed(service, STALE, age=45.0)

    result = await service.cached_request(KEY, 30.0, request, stale_grace=60.0)

    assert result == STALE
    background = service.inflight.get(KEY)
    assert background is not None
    await asyncio.shield(background)
    assert request.calls == 1

    # The background refresh must have replaced the cached payload.
    follow_up = FakeRequest([{"success": True, "value": "should-not-run"}])
    again = await service.cached_request(KEY, 30.0, follow_up)
    assert again == FRESH
    assert follow_up.calls == 0


@pytest.mark.asyncio
async def test_stale_beyond_grace_blocks_on_fresh_fetch() -> None:
    service = SteamDTService()
    request = FakeRequest([FRESH])
    _seed(service, STALE, age=200.0)

    result = await service.cached_request(KEY, 30.0, request, stale_grace=60.0)

    assert result == FRESH
    assert request.calls == 1


@pytest.mark.asyncio
async def test_zero_grace_disables_stale_serving() -> None:
    service = SteamDTService()
    request = FakeRequest([FRESH])
    _seed(service, STALE, age=31.0)

    result = await service.cached_request(KEY, 30.0, request, stale_grace=0.0)

    assert result == FRESH


@pytest.mark.asyncio
async def test_background_refresh_failure_keeps_stale_value() -> None:
    service = SteamDTService()
    request = FakeRequest([RuntimeError("upstream down"), RuntimeError("upstream down 2")])
    _seed(service, STALE, age=45.0)

    result = await service.cached_request(KEY, 30.0, request, stale_grace=60.0)
    assert result == STALE
    background = service.inflight.get(KEY)
    assert background is not None
    with pytest.raises(RuntimeError):
        await asyncio.shield(background)

    # The stale entry survives and is served again on the next call; errors
    # were never cached, so a new background refresh is attempted each time.
    second = await service.cached_request(KEY, 30.0, request, stale_grace=60.0)
    assert second == STALE
    retry = service.inflight.get(KEY)
    assert retry is not None, "a failed refresh must be retried on the next stale read"
    with pytest.raises(RuntimeError):
        await asyncio.shield(retry)
    assert request.calls == 2


@pytest.mark.asyncio
async def test_concurrent_stale_callers_share_one_background_refresh() -> None:
    service = SteamDTService()

    class PausableRequest:
        """Real upstream request held suspended until the test releases it."""

        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self) -> dict[str, Any]:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return FRESH

    request = PausableRequest()
    _seed(service, STALE, age=45.0)

    # Every task this test owns is registered the moment it is created, so
    # the finally block can always release and reap them — even when an
    # assertion above fails before `background` was first read.
    owned: list[asyncio.Task[Any]] = []
    first: asyncio.Task[dict[str, Any]] | None = None
    second: asyncio.Task[dict[str, Any]] | None = None
    background: asyncio.Task[dict[str, Any]] | None = None
    try:
        first = asyncio.create_task(service.cached_request(KEY, 30.0, request, stale_grace=60.0))
        owned.append(first)
        assert await asyncio.wait_for(request.started.wait(), timeout=5), (
            "background refresh never started"
        )
        # The refresh is now suspended mid-request; grab its inflight task
        # before any assertion can fail, then bring in the second caller.
        background = service.inflight.get(KEY)
        assert background is not None, "the suspended refresh must stay registered"
        owned.append(background)
        second = asyncio.create_task(service.cached_request(KEY, 30.0, request, stale_grace=60.0))
        owned.append(second)
        assert (await first) == STALE
        assert (await second) == STALE
        assert request.calls == 1, "concurrent callers must not stampede the refresh"

        # Release the upstream: the actual background task writes the fresh
        # cache and its done-callback clears the inflight registration.
        request.release.set()
        assert await asyncio.wait_for(background, timeout=5) == FRESH
        for _ in range(3):
            await asyncio.sleep(0)  # let the done-callback run on the loop
        assert service.inflight.get(KEY) is None, "inflight entry must be cleared by cleanup"
        age = service.cache_age(KEY)
        assert age is not None and age < 5.0, "the fresh result must be cached"
        assert service.cache.get(KEY)[1] == FRESH, "cache must hold the fresh payload itself"
    finally:
        # Failure paths must still release the suspended request and reap
        # every task this test owns (never leave them dangling on the loop).
        # If the wait above timed out before `background` was read, the
        # already-created refresh is recovered from the real inflight
        # registry; the set de-duplicates it against `owned`.  Tasks that
        # already finished are awaited too, so their exceptions are read
        # and can never surface as unhandled-task warnings.  Cleanup never
        # swallows verification failures.
        request.release.set()
        reaped: dict[int, asyncio.Task[Any]] = {
            id(task): task for task in (background, *owned) if task is not None
        }
        late = service.inflight.get(KEY)
        if late is not None:
            reaped.setdefault(id(late), late)
        tasks = list(reaped.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_foreground_fetch_is_never_cached() -> None:
    service = SteamDTService()
    request = FakeRequest([{"success": False, "errorMsg": "refused"}, FRESH])

    refused = await service.cached_request(KEY, 30.0, request)
    assert refused["success"] is False

    retried = await service.cached_request(KEY, 30.0, request)
    assert retried == FRESH
    assert request.calls == 2
    assert service.cache_age(KEY) is not None


@pytest.mark.asyncio
async def test_cache_age_returns_none_for_unknown_entries() -> None:
    service = SteamDTService()
    assert service.cache_age("GET:unknown") is None
    assert market_cache_age("GET:unknown") is None

    _seed(service, FRESH, age=0.0)
    age = service.cache_age(KEY)
    assert age is not None and age < 5.0


@pytest.mark.asyncio
async def test_cached_request_ttl_bounds_apply_to_stale_grace_reads() -> None:
    """A grace read must not resurrect entries that were never cached."""

    service = SteamDTService()
    request = FakeRequest([FRESH])
    result = await service.cached_request(KEY, 30.0, request, stale_grace=120.0)
    assert result == FRESH
    assert service.inflight.get(KEY) is None
