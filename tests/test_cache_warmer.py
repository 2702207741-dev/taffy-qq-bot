from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.services import cache_warmer


@pytest.mark.asyncio
async def test_fresh_volume_cache_does_not_start_browser(tmp_path: Path) -> None:
    cache = tmp_path / "volume.json"
    cache.write_text("{}", encoding="utf-8")
    now = cache.stat().st_mtime
    runner = AsyncMock(return_value=True)
    with (
        patch.object(cache_warmer, "steamdt_cache_path", return_value=cache),
        patch.object(cache_warmer, "run_cache_warmer_once", runner),
    ):
        assert await cache_warmer.ensure_volume_cache_fresh(now=now)

    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_stale_requests_share_one_browser_refresh(tmp_path: Path) -> None:
    cache = tmp_path / "volume.json"
    release = asyncio.Event()
    calls = 0
    now = 1_800_000_000.0

    async def refresh() -> bool:
        nonlocal calls
        calls += 1
        await release.wait()
        cache.write_text("{}", encoding="utf-8")
        os.utime(cache, (now, now))
        return True

    with (
        patch.object(cache_warmer, "steamdt_cache_path", return_value=cache),
        patch.object(cache_warmer, "run_cache_warmer_once", side_effect=refresh),
    ):
        first = asyncio.create_task(cache_warmer.ensure_volume_cache_fresh(now=now))
        second = asyncio.create_task(cache_warmer.ensure_volume_cache_fresh(now=now))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == [True, True]

    assert calls == 1
