from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.services import steamdt_kline
from src.services.steamdt_kline import (
    KlineScreenshotConfig,
    KlineScreenshotError,
    SteamDTKlineScreenshotService,
    _parse_kline_points,
    capture_broad_kline_hd_png,
    capture_item_kline_hd_png,
    capture_item_kline_png,
    validate_kline_png,
)


def _kline_response(latest: float = 1_800_000_000) -> dict[str, object]:
    start = latest - 39 * 86_400
    return {
        "success": True,
        "data": [
            [str(start + index * 86_400), 900 + index, 902 + index, 905 + index, 898 + index]
            for index in range(40)
        ],
    }


def _png(width: int = 1440, height: int = 1000, *, size: int = 2_048) -> bytes:
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


def _config(*, timeout: float = 1.0) -> KlineScreenshotConfig:
    return KlineScreenshotConfig(
        timeout_seconds=timeout,
        terminate_grace_seconds=0.1,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )


def test_png_validation_requires_complete_full_size_image() -> None:
    self_contained = _png()
    assert validate_kline_png(self_contained, max_bytes=5 * 1024 * 1024) == (1440, 1000)

    with pytest.raises(KlineScreenshotError):
        validate_kline_png(b"not-png", max_bytes=5 * 1024 * 1024)
    with pytest.raises(KlineScreenshotError):
        validate_kline_png(_png(width=850, height=540), max_bytes=5 * 1024 * 1024)
    with pytest.raises(KlineScreenshotError):
        validate_kline_png(self_contained[:-12], max_bytes=5 * 1024 * 1024)


def test_kline_input_accepts_numeric_string_timestamps_and_rejects_disorder() -> None:
    latest = 1_800_000_000
    points = _parse_kline_points(_kline_response(latest), now=latest)
    assert len(points) == 40
    assert points[-1][0] == latest

    disorder = _kline_response(latest)
    disorder["data"][10], disorder["data"][11] = (  # type: ignore[index]
        disorder["data"][11],  # type: ignore[index]
        disorder["data"][10],  # type: ignore[index]
    )
    with pytest.raises(KlineScreenshotError, match="strictly ordered"):
        _parse_kline_points(disorder, now=latest)

    ohlc = _kline_response(latest)
    ohlc["data"] = [  # type: ignore[index]
        [row[0], row[1], row[3], row[4], row[2]]
        for row in ohlc["data"]  # type: ignore[index]
    ]
    normalized = _parse_kline_points(ohlc, now=latest)
    assert normalized[-1][1:] == points[-1][1:]


def test_kline_input_normalizes_only_small_ohlc_envelope_drift() -> None:
    latest = 1_800_000_000
    small_drift = _kline_response(latest)
    small_row = small_drift["data"][10]  # type: ignore[index]
    small_row[4] = small_row[1] + 1.8  # type: ignore[index,operator]

    normalized = _parse_kline_points(small_drift, now=latest)

    assert normalized[10][4] == normalized[10][1]

    large_drift = _kline_response(latest)
    large_row = large_drift["data"][10]  # type: ignore[index]
    large_row[4] = large_row[1] + 3.0  # type: ignore[index,operator]
    with pytest.raises(KlineScreenshotError, match="OHLC relationship"):
        _parse_kline_points(large_drift, now=latest)


@pytest.mark.asyncio
async def test_concurrent_requests_share_capture_and_use_cache() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    now = 100.0
    png = _png()

    async def capture() -> bytes:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return png

    service = SteamDTKlineScreenshotService(
        capture=capture,
        clock=lambda: now,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )
    pending = [asyncio.create_task(service.get_broad_png()) for _ in range(20)]
    await started.wait()
    release.set()

    assert await asyncio.gather(*pending) == [png] * 20
    assert await service.get_broad_png() == png
    assert calls == 1

    now = 161.0
    assert await service.get_broad_png() == png
    assert calls == 2
    await service.close()


@pytest.mark.asyncio
async def test_item_requests_share_per_item_capture_and_cache() -> None:
    calls: list[str] = []
    png = _png()

    async def item_capture(market_name: str) -> bytes:
        calls.append(market_name)
        return png

    service = SteamDTKlineScreenshotService(
        item_capture=item_capture,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )
    market_name = "AK-47 | B the Monster (Factory New)"

    results = await asyncio.gather(*(service.get_item_png(market_name) for _ in range(10)))

    assert results == [png] * 10
    assert calls == [market_name]
    assert await service.get_item_png("AWP | Asiimov (Field-Tested)") == png
    assert calls == [market_name, "AWP | Asiimov (Field-Tested)"]
    assert not service._item_inflight
    await service.close()


@pytest.mark.asyncio
async def test_special_styles_use_separate_cache_keys_and_reject_unknown_codes() -> None:
    calls: list[tuple[str, str | None]] = []
    png = _png()

    async def item_capture(
        market_name: str,
        _config: KlineScreenshotConfig | None = None,
        *,
        special_style: str | None = None,
    ) -> bytes:
        calls.append((market_name, special_style))
        return png

    service = SteamDTKlineScreenshotService(
        item_capture=item_capture,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )
    name = "★ Karambit | Doppler (Factory New)"

    assert await service.get_item_png(name, special_style="ruby") == png
    assert await service.get_item_png(name, special_style="ruby") == png
    assert await service.get_item_png(name, special_style="p2") == png
    assert calls == [(name, "ruby"), (name, "p2")]
    with pytest.raises(KlineScreenshotError, match="special style"):
        await service.get_item_png(name, special_style="unknown")
    await service.close()


@pytest.mark.asyncio
async def test_fast_item_capture_sends_special_style_to_kline_endpoint() -> None:
    market_post = AsyncMock(return_value=_kline_response())
    name = "★ Karambit | Doppler (Factory New)"

    result = await capture_item_kline_png(
        name,
        _config(),
        special_style="ruby",
        market_post=market_post,
        wall_clock=lambda: 1_800_000_000,
    )

    validate_kline_png(result, max_bytes=5 * 1024 * 1024)
    market_post.assert_awaited_once_with(
        "/open/cs2/item/v1/kline",
        {"marketHashName": name, "type": 2, "specialStyle": "ruby"},
    )


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_capture() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    capture_cancelled = False
    png = _png()

    async def capture() -> bytes:
        nonlocal capture_cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            capture_cancelled = True
            raise
        return png

    service = SteamDTKlineScreenshotService(
        capture=capture,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )
    first = asyncio.create_task(service.get_broad_png())
    second = asyncio.create_task(service.get_broad_png())
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert not capture_cancelled
    release.set()
    assert await second == png
    await service.close()


@pytest.mark.asyncio
async def test_failed_capture_does_not_poison_next_attempt() -> None:
    calls = 0
    png = _png()

    async def capture() -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KlineScreenshotError("temporary failure")
        return png

    service = SteamDTKlineScreenshotService(
        capture=capture,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )
    with pytest.raises(KlineScreenshotError):
        await service.get_broad_png()

    assert await service.get_broad_png() == png
    assert calls == 2
    await service.close()


@pytest.mark.asyncio
async def test_worker_output_is_read_validated_and_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    png = _png()
    output_path: Path | None = None
    captured_environment: dict[str, str] = {}

    class Process:
        returncode: int | None = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    async def create_process(*arguments: object, **kwargs: object) -> Process:
        nonlocal output_path, captured_environment
        output_path = Path(str(arguments[2]))
        output_path.write_bytes(png)
        captured_environment = dict(kwargs["env"])  # type: ignore[arg-type]
        return Process()

    monkeypatch.setenv("TAFFY_API_KEY", "must-not-leak")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    async def fetcher() -> dict[str, object]:
        return _kline_response()

    assert (
        await capture_broad_kline_hd_png(
            _config(), fetcher=fetcher, wall_clock=lambda: 1_800_000_000
        )
        == png
    )
    assert output_path is not None and not output_path.exists()
    assert "TAFFY_API_KEY" not in captured_environment


@pytest.mark.asyncio
async def test_item_capture_uses_resolved_name_in_endpoint_and_chart_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = _png()
    payload: dict[str, object] = {}

    class Process:
        returncode: int | None = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    async def create_process(*arguments: object, **kwargs: object) -> Process:
        del kwargs
        output = Path(str(arguments[2]))
        data_file = Path(str(arguments[3]))
        payload.update(json.loads(data_file.read_text(encoding="utf-8")))
        output.write_bytes(png)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    market_post = AsyncMock(return_value=_kline_response())
    market_name = "AK-47 | B the Monster (Factory New)"

    result = await capture_item_kline_hd_png(
        market_name,
        _config(),
        market_post=market_post,
        wall_clock=lambda: 1_800_000_000,
    )

    assert result == png
    market_post.assert_awaited_once_with(
        "/open/cs2/item/v1/kline",
        {"marketHashName": market_name, "type": 2},
    )
    assert payload["title"] == f"塔菲 · {market_name}"


@pytest.mark.asyncio
async def test_worker_timeout_terminates_process_and_removes_partial_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = asyncio.Event()
    output_path: Path | None = None

    class Process:
        returncode: int | None = None
        pid = 12345

        async def wait(self) -> int:
            await stopped.wait()
            return self.returncode or 0

        def terminate(self) -> None:
            self.returncode = -15
            stopped.set()

        def kill(self) -> None:
            self.returncode = -9
            stopped.set()

    async def create_process(*arguments: object, **kwargs: object) -> Process:
        nonlocal output_path
        del kwargs
        output_path = Path(str(arguments[2]))
        output_path.write_bytes(b"partial")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    async def fetcher() -> dict[str, object]:
        return _kline_response()

    with pytest.raises(KlineScreenshotError, match="timed out"):
        await capture_broad_kline_hd_png(
            _config(timeout=0.01),
            fetcher=fetcher,
            wall_clock=lambda: 1_800_000_000,
        )

    assert stopped.is_set()
    assert output_path is not None and not output_path.exists()


@pytest.mark.asyncio
async def test_close_cancels_inflight_capture() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def capture() -> bytes:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return _png()

    service = SteamDTKlineScreenshotService(
        capture=capture,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )
    waiter = asyncio.create_task(service.get_broad_png())
    await started.wait()
    await service.close()

    assert cancelled.is_set()
    with pytest.raises(asyncio.CancelledError):
        await waiter


def test_worker_path_is_fixed_inside_project() -> None:
    assert steamdt_kline._WORKER == (
        steamdt_kline.PROJECT_ROOT / "src" / "services" / "steamdt_kline_worker.py"
    )


# ---------------------------------------------------------------------------
# SteamDT 未用接口批D：分平台 K 线 + 大盘 maxTime 回溯
# ---------------------------------------------------------------------------


def test_kline_platform_suffix_parsing() -> None:
    assert steamdt_kline.validated_kline_platform("BUFF") == "BUFF"
    assert steamdt_kline.validated_kline_platform("悠悠") == "YOUPIN"
    assert steamdt_kline.validated_kline_platform("HALO") == "HALOSKINS"
    assert steamdt_kline.validated_kline_platform("xx") is None
    assert steamdt_kline.validated_kline_platform(None) is None
    assert steamdt_kline.parse_kline_platform_suffix("AK-47 BUFF") == ("AK-47", "BUFF")
    assert steamdt_kline.parse_kline_platform_suffix("蝴蝶刀 悠悠") == ("蝴蝶刀", "YOUPIN")
    assert steamdt_kline.parse_kline_platform_suffix("红线") == ("红线", None)
    assert steamdt_kline.parse_kline_platform_suffix("BUFF") == ("BUFF", None)
    assert steamdt_kline.parse_kline_platform_suffix("AK XYZ") == ("AK XYZ", None)
    assert steamdt_kline.parse_kline_platform_suffix("AK C5") == ("AK", "C5")


@pytest.mark.asyncio
async def test_fast_item_capture_sends_platform_to_kline_endpoint() -> None:
    market_post = AsyncMock(return_value=_kline_response())
    name = "AK-47 | Redline (Field-Tested)"

    result = await capture_item_kline_png(
        name,
        _config(),
        platform="BUFF",
        market_post=market_post,
        wall_clock=lambda: 1_800_000_000,
    )

    validate_kline_png(result, max_bytes=5 * 1024 * 1024)
    market_post.assert_awaited_once_with(
        "/open/cs2/item/v1/kline",
        {"marketHashName": name, "type": 2, "platform": "BUFF"},
    )
    with pytest.raises(KlineScreenshotError, match="platform"):
        await capture_item_kline_png(name, _config(), platform="xx", market_post=market_post)


@pytest.mark.asyncio
async def test_platforms_use_separate_cache_keys() -> None:
    calls: list[tuple[str, str | None]] = []
    png = _png()

    async def item_capture(
        market_name: str,
        *,
        platform: str | None = None,
    ) -> bytes:
        calls.append((market_name, platform))
        return png

    service = SteamDTKlineScreenshotService(
        item_capture=item_capture,
        cache_seconds=60.0,
        max_bytes=5 * 1024 * 1024,
    )
    name = "AK-47 | Redline (Field-Tested)"

    assert await service.get_item_png(name, platform="BUFF") == png
    assert await service.get_item_png(name, platform="BUFF") == png
    assert await service.get_item_png(name) == png
    assert calls == [(name, "BUFF"), (name, None)]
    with pytest.raises(KlineScreenshotError, match="platform"):
        await service.get_item_png(name, platform="xx")
    await service.close()


@pytest.mark.asyncio
async def test_broad_kline_posts_max_time_for_backtest() -> None:
    from unittest.mock import patch

    market_post = AsyncMock(return_value={"success": True, "data": []})
    with patch.object(steamdt_kline, "st_post", market_post):
        await steamdt_kline.fetch_broad_kline(max_time=1_700_000_000)
    market_post.assert_awaited_once_with(
        "/open/cs2/broad/v1/kline", {"type": 2, "maxTime": 1_700_000_000}
    )


@pytest.mark.asyncio
async def test_broad_backtest_rejects_out_of_range() -> None:
    with pytest.raises(KlineScreenshotError, match="lookback"):
        await steamdt_kline.capture_broad_backtest_png(0)
    with pytest.raises(KlineScreenshotError, match="lookback"):
        await steamdt_kline.capture_broad_backtest_png(91)
