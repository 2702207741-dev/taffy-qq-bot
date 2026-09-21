from __future__ import annotations

import json
from collections.abc import Coroutine
from pathlib import Path
from unittest.mock import patch

import pytest

from src.services import steamdt_kline_worker as worker


def _points(count: int = 35) -> list[list[float]]:
    return [
        [1_700_000_000 + index * 86400, 100 + index, 101 + index, 103 + index, 99 + index]
        for index in range(count)
    ]


def _payload(tmp_path: Path, *, title: str = "塔菲 K线", points: object = None) -> Path:
    path = tmp_path / "data.json"
    path.write_text(
        json.dumps({"title": title, "points": _points() if points is None else points}),
        encoding="utf-8",
    )
    return path


def test_worker_arguments_require_safe_absolute_paths(tmp_path: Path) -> None:
    output = (tmp_path / "out.png").resolve()
    data = _payload(tmp_path).resolve()
    assert worker._arguments(["worker", str(output), str(data)]) == (output, data)
    for arguments in (
        ["worker"],
        ["worker", "relative.png", str(data)],
        ["worker", str(tmp_path / "out.txt"), str(data)],
        ["worker", str(output), str(tmp_path / "missing.json")],
    ):
        with pytest.raises(ValueError):
            worker._arguments(arguments)


def test_worker_payload_validation_and_history_bound(tmp_path: Path) -> None:
    title, points = worker._load_payload(_payload(tmp_path, points=_points(100)))
    assert title == "塔菲 K线" and len(points) == 90
    invalid_payloads: tuple[object, ...] = (
        [],
        {"title": "", "points": _points()},
        {"title": "ok", "points": "bad"},
        {"title": "ok", "points": [[1, 2]] * 30},
        {"title": "ok", "points": [[1, 2, 3, 4, 0]] * 30},
        {"title": "ok", "points": _points(29)},
    )
    for index, payload in enumerate(invalid_payloads):
        path = tmp_path / f"invalid-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises((ValueError, TypeError)):
            worker._load_payload(path)
    huge = tmp_path / "huge.json"
    huge.write_bytes(b" " * 1_000_001)
    with pytest.raises(ValueError, match="too large"):
        worker._load_payload(huge)


def test_worker_chart_is_local_escaped_and_contains_real_ohlc() -> None:
    document = worker._chart_html("<script>alert(1)</script>", _points())
    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "7日均线" in document and "30日均线" in document
    assert "历史走势仅供数据观察" in document
    polyline = worker._polyline(_points(), 7, lambda index: index, lambda value: value)
    assert len(polyline.split()) == len(_points()) - 6


@pytest.mark.asyncio
async def test_worker_render_closes_browser_and_writes_png(tmp_path: Path) -> None:
    events: list[str] = []

    class Page:
        async def set_content(self, document: str, **_kwargs: object) -> None:
            assert "<svg" in document

        async def screenshot(self, **_kwargs: object) -> bytes:
            return b"png-data"

    class Context:
        async def new_page(self) -> Page:
            return Page()

    class Browser:
        async def new_context(self, **_kwargs: object) -> Context:
            return Context()

        async def close(self) -> None:
            events.append("closed")

    class Chromium:
        async def launch(self, **_kwargs: object) -> Browser:
            return Browser()

    class Playwright:
        chromium = Chromium()

    class Manager:
        async def __aenter__(self) -> Playwright:
            return Playwright()

        async def __aexit__(self, *_args: object) -> None:
            return None

    output = tmp_path / "out.png"
    data = _payload(tmp_path)
    with patch.object(worker, "async_playwright", return_value=Manager()):
        await worker.render_kline(output, data)
    assert output.read_bytes() == b"png-data"
    assert events == ["closed"]


def test_worker_main_returns_bounded_exit_codes(tmp_path: Path) -> None:
    output = (tmp_path / "out.png").resolve()
    data = _payload(tmp_path).resolve()

    def close_coroutine(coroutine: Coroutine[object, object, object]) -> None:
        coroutine.close()

    with patch.object(worker.asyncio, "run", side_effect=close_coroutine) as run:
        assert worker.main(["worker", str(output), str(data)]) == 0
        run.assert_called_once()
    assert worker.main(["worker"]) == 1
