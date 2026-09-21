"""Batch 5: historical K-line backtests must honor their cutoff time.

A ``capture_broad_backtest_png(max_days_ago=N)`` run computes one cutoff
``cutoff = int(wall_clock()) - N*86400`` and uses that same value for the
upstream ``maxTime`` request and for every later validation.  Data whose
last point lies after the cutoff must never reach the renderers, and a
failed historical request must not silently fall back to current quotes.

The suite drives the real ``capture_broad_backtest_png``,
``fetch_broad_kline`` and ``_capture_kline_png`` with real parsing and
time checks; only the network boundary (``st_post`` / ``request_json``)
and the external render boundaries (fast renderer call, browser worker
subprocess) are replaced.  No real browser, subprocess or network.

Fixed constants::

    T = 1_800_000_000            (wall clock)
    N = 30
    C = T - 30 * 86_400 = 1_797_408_000
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.services import steamdt_kline
from src.services.steamdt_kline import (
    KlineScreenshotConfig,
    KlineScreenshotError,
    capture_broad_backtest_png,
)

T = 1_800_000_000
N = 30
C = T - N * 86_400  # 1_797_408_000
DAYS = 86_400


def _kline_response(latest: float) -> dict[str, object]:
    """40 valid daily rows, strictly increasing by one day, ending at
    ``latest`` (numeric timestamps as strings, mirroring the API)."""
    start = latest - 39 * DAYS
    return {
        "success": True,
        "data": [
            [str(start + index * DAYS), 900 + index, 902 + index, 905 + index, 898 + index]
            for index in range(40)
        ],
    }


def _png_bytes(width: int = 1440, height: int = 1000, *, size: int = 2_048) -> bytes:
    header = (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    trailer = b"\x00\x00\x00\x00IEND\xaeB`\x82"
    return header + bytes(max(0, size - len(header) - len(trailer))) + trailer


def _config() -> KlineScreenshotConfig:
    return KlineScreenshotConfig(
        timeout_seconds=1.0,
        terminate_grace_seconds=0.1,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )


def _call_count(mock: AsyncMock) -> int:
    return len(mock.await_args_list) if mock.await_args_list else 0


# ---------------------------------------------------------------------------
# Fast-render boundary: wrap the real renderer to record the points it got.
# ---------------------------------------------------------------------------


def _install_fast_recorder(monkeypatch: pytest.MonkeyPatch) -> list[list[list[float]]]:
    """Record the points handed to the fast renderer, then call the real
    renderer so the PNG validation stays real."""
    received: list[list[list[float]]] = []
    real_render = steamdt_kline.render_kline_png

    def recording_render(title: str, points: list[list[float]]) -> bytes:
        received.append([list(point) for point in points])
        return real_render(title, points)

    monkeypatch.setattr(steamdt_kline, "render_kline_png", recording_render)
    return received


class _Process:
    returncode: int | None = None
    pid = 12345

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _install_hd_worker(monkeypatch: pytest.MonkeyPatch) -> list[list[list[float]]]:
    """Replace the browser worker subprocess: read the temp JSON the real
    code writes, record the points, and write a valid PNG to the output
    path.  No real browser or subprocess starts."""
    received: list[list[list[float]]] = []

    async def fake_subprocess(*arguments: object, **kwargs: object) -> _Process:
        del kwargs
        output_path = Path(str(arguments[2]))
        data_file = Path(str(arguments[3]))
        payload = json.loads(data_file.read_text(encoding="utf-8"))
        received.append([list(point) for point in payload["points"]])
        output_path.write_bytes(_png_bytes())
        return _Process()

    monkeypatch.setattr(steamdt_kline.asyncio, "create_subprocess_exec", fake_subprocess)
    return received


async def _run_backtest(
    *,
    latest: float,
    wall_clock: float = T,
    max_days_ago: int = N,
    market_post: AsyncMock | None = None,
    high_definition: bool = False,
) -> bytes:
    """Drive the real backtest capture against a stubbed network."""
    post = market_post or AsyncMock(return_value=_kline_response(latest))
    with patch.object(steamdt_kline, "st_post", post):
        return await capture_broad_backtest_png(
            max_days_ago,
            high_definition=high_definition,
            wall_clock=lambda: wall_clock,
        )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_history_requests_cutoff_and_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last point at C succeeds; the upstream request carries maxTime=C and
    the data handed to the renderer ends exactly at C."""
    received = _install_fast_recorder(monkeypatch)
    post = AsyncMock(return_value=_kline_response(C))
    png = await _run_backtest(latest=C, market_post=post)
    assert png  # real fast renderer produced a valid PNG
    post.assert_awaited_once_with("/open/cs2/broad/v1/kline", {"type": 2, "maxTime": C})
    assert received, "fast renderer was not reached"
    assert received[0][-1][0] == C


@pytest.mark.asyncio
async def test_normal_history_hd_worker_receives_same_cutoff_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High-definition backtest passes the same cutoff boundary; the worker
    JSON carries points ending exactly at C."""
    received = _install_hd_worker(monkeypatch)
    post = AsyncMock(return_value=_kline_response(C))
    png = await _run_backtest(latest=C, market_post=post, high_definition=True)
    assert png == _png_bytes()
    assert received, "hd worker was not reached"
    assert received[0][-1][0] == C
    assert received[0][0][0] == C - 39 * DAYS


@pytest.mark.asyncio
async def test_last_point_one_second_after_cutoff_rejected_before_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C+1s passes a cutoff-relative parse (fresh) but must still be
    rejected by the strict ceiling before any renderer runs — this catches
    an implementation that only moves the parse clock and forgets the
    hard cutoff check."""
    received = _install_fast_recorder(monkeypatch)
    post = AsyncMock(return_value=_kline_response(C + 1))
    with pytest.raises(KlineScreenshotError):
        await _run_backtest(latest=C + 1, market_post=post)
    assert not received, "fast renderer ran despite data after the cutoff"


@pytest.mark.asyncio
async def test_millisecond_timestamp_after_cutoff_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(C+1)*1000 ms normalizes to C+1 s and must be rejected too — the
    millisecond normalization in _parse_kline_points must not bypass the
    cutoff ceiling."""
    received = _install_fast_recorder(monkeypatch)
    post = AsyncMock(return_value=_kline_response((C + 1) * 1000))
    with pytest.raises(KlineScreenshotError):
        await _run_backtest(latest=(C + 1) * 1000, market_post=post)
    assert not received, "fast renderer ran despite ms data after the cutoff"


@pytest.mark.asyncio
async def test_hd_also_rejects_after_cutoff_before_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = _install_hd_worker(monkeypatch)
    post = AsyncMock(return_value=_kline_response(C + 1))
    with pytest.raises(KlineScreenshotError):
        await _run_backtest(latest=C + 1, market_post=post, high_definition=True)
    assert not received, "hd worker ran despite data after the cutoff"


@pytest.mark.asyncio
async def test_last_point_three_days_before_cutoff_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C - 3 days is the freshness boundary relative to cutoff: accepted."""
    received = _install_fast_recorder(monkeypatch)
    post = AsyncMock(return_value=_kline_response(C - 3 * DAYS))
    png = await _run_backtest(latest=C - 3 * DAYS, market_post=post)
    assert png
    assert received and received[0][-1][0] == C - 3 * DAYS


@pytest.mark.asyncio
async def test_last_point_three_days_minus_one_second_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C - 3 days - 1s is stale relative to cutoff: rejected before render."""
    received = _install_fast_recorder(monkeypatch)
    post = AsyncMock(return_value=_kline_response(C - 3 * DAYS - 1))
    with pytest.raises(KlineScreenshotError):
        await _run_backtest(latest=C - 3 * DAYS - 1, market_post=post)
    assert not received, "fast renderer ran despite stale history"


@pytest.mark.asyncio
async def test_upstream_ignoring_maxtime_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upstream ignores maxTime and returns data ending at T (today):
    the backtest must reject it instead of drawing a chart that claims to
    end 30 days ago."""
    received = _install_fast_recorder(monkeypatch)
    post = AsyncMock(return_value=_kline_response(T))
    with pytest.raises(KlineScreenshotError):
        await _run_backtest(latest=T, market_post=post)
    assert not received, "fast renderer ran despite upstream ignoring maxTime"


@pytest.mark.asyncio
async def test_historical_primary_failure_does_not_call_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the historical (maxTime) primary fails, fetch_broad_kline must
    return that failure directly: the current-quote fallback (request_json)
    is not called at all."""
    request_mock = AsyncMock(return_value={"success": False})
    monkeypatch.setattr(steamdt_kline, "request_json", request_mock)
    post = AsyncMock(return_value={"success": False, "error": "quota"})
    with patch.object(steamdt_kline, "st_post", post):
        response = await steamdt_kline.fetch_broad_kline(max_time=C)
    assert response.get("success") is False
    assert _call_count(request_mock) == 0, "fallback must not run for history requests"


@pytest.mark.asyncio
async def test_current_quote_failure_still_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary current-quote fetch (max_time=None) keeps its fallback: when
    the primary fails and the fallback returns valid current data, capture
    still succeeds through the original path."""
    received = _install_fast_recorder(monkeypatch)

    # The fallback first fetches the web config (for the checksum
    # timestamp) and then the kline series: two request_json calls.
    async def fake_request_json(method: str, url: str, **kwargs: object) -> dict[str, object]:
        del method, kwargs
        if url.endswith("/user/system-config/v1/default-config"):
            return {"success": True, "data": {"systemTime": "1797408000000"}}
        if url.endswith("/user/statistics/v1/kline"):
            return {"success": True, "data": _kline_response(T)["data"]}
        raise AssertionError(f"unexpected fallback url: {url}")

    monkeypatch.setattr(steamdt_kline, "request_json", fake_request_json)
    post = AsyncMock(return_value={"success": False})
    with patch.object(steamdt_kline, "st_post", post):
        # Capture ordinary broad chart (current) through _capture_kline_png.
        png = await steamdt_kline._capture_kline_png(
            "塔菲 · CS2 市场日K",
            _config(),
            fetcher=steamdt_kline.fetch_broad_kline,
            wall_clock=lambda: T,
        )
    assert png
    assert received and received[0][-1][0] == T


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 29])
async def test_empty_and_short_history_rejected_without_render(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    received = _install_fast_recorder(monkeypatch)
    data = _kline_response(C)["data"][:count] if count else []
    post = AsyncMock(return_value={"success": True, "data": data})
    with pytest.raises(KlineScreenshotError):
        await _run_backtest(latest=C, market_post=post)
    assert not received, "renderer ran despite empty/short history"
