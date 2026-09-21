"""One-shot Playwright worker that renders validated OHLC data locally."""

from __future__ import annotations

import asyncio
import html
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import ViewportSize, async_playwright

from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

VIEWPORT: ViewportSize = {"width": 1440, "height": 1000}


def _arguments(arguments: list[str]) -> tuple[Path, Path]:
    if len(arguments) != 3:
        raise ValueError("exactly one output and one data path are required")
    output = Path(arguments[1])
    data_file = Path(arguments[2])
    if not output.is_absolute() or output.suffix.casefold() != ".png":
        raise ValueError("output must be an absolute PNG path")
    if not data_file.is_absolute() or data_file.suffix.casefold() != ".json":
        raise ValueError("data must be an absolute JSON path")
    if not output.parent.is_dir() or not data_file.is_file():
        raise ValueError("render paths are unavailable")
    return output, data_file


def _load_payload(data_file: Path) -> tuple[str, list[list[float]]]:
    if data_file.stat().st_size > 1_000_000:
        raise ValueError("render data is too large")
    payload = json.loads(data_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("render payload is invalid")
    title = str(payload.get("title", "")).strip()
    raw_points = payload.get("points")
    if not title or len(title) > 100 or not isinstance(raw_points, list):
        raise ValueError("render payload is incomplete")
    points: list[list[float]] = []
    for raw in raw_points:
        if not isinstance(raw, list) or len(raw) != 5:
            raise ValueError("render point is invalid")
        values = [float(value) for value in raw]
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("render value is invalid")
        points.append(values)
    if len(points) < 30:
        raise ValueError("render history is insufficient")
    return title, points[-90:]


def _polyline(
    points: list[list[float]],
    period: int,
    x_of: Any,
    y_of: Any,
) -> str:
    coordinates: list[str] = []
    for index in range(period - 1, len(points)):
        average = sum(point[2] for point in points[index - period + 1 : index + 1]) / period
        coordinates.append(f"{x_of(index):.2f},{y_of(average):.2f}")
    return " ".join(coordinates)


def _chart_html(title: str, points: list[list[float]]) -> str:
    width, height = 1440, 1000
    left, right, top, bottom = 100, 45, 170, 95
    chart_width = width - left - right
    chart_height = height - top - bottom
    low = min(point[4] for point in points)
    high = max(point[3] for point in points)
    padding = max((high - low) * 0.06, high * 0.002)
    low -= padding
    high += padding
    step = chart_width / len(points)
    candle_width = max(3.0, min(10.0, step * 0.62))

    def x_of(index: int) -> float:
        return left + step * (index + 0.5)

    def y_of(price: float) -> float:
        return top + (high - price) / (high - low) * chart_height

    svg: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#0b1220"/>',
        '<rect x="24" y="24" width="1392" height="952" rx="24" fill="#111a2b" stroke="#263650"/>',
        f'<text x="{left}" y="72" fill="#f2f6ff" font-size="34" font-weight="700">{html.escape(title)}</text>',
        '<text x="100" y="112" fill="#8fa3bf" font-size="18">真实日K · 本地绘制 · 红涨绿跌</text>',
    ]

    latest = points[-1]
    change = (latest[2] / latest[1] - 1) * 100
    latest_date = datetime.fromtimestamp(latest[0], _MARKET_TZ).strftime("%Y-%m-%d")
    svg.extend(
        (
            f'<text x="100" y="145" fill="#c8d5e8" font-size="18">数据日期 {latest_date}　开 {latest[1]:.2f}　收 {latest[2]:.2f}　高 {latest[3]:.2f}　低 {latest[4]:.2f}</text>',
            f'<text x="1320" y="95" text-anchor="end" fill="{"#ef5350" if change >= 0 else "#26a69a"}" font-size="30" font-weight="700">{change:+.2f}%</text>',
        )
    )

    for grid_index in range(7):
        ratio = grid_index / 6
        y = top + chart_height * ratio
        price = high - (high - low) * ratio
        svg.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" stroke="#263650" stroke-width="1"/>'
        )
        svg.append(
            f'<text x="{left - 14}" y="{y + 6:.2f}" text-anchor="end" fill="#8294ad" font-size="16">{price:.2f}</text>'
        )

    label_indexes = sorted(
        {0, len(points) - 1, *(round(i * (len(points) - 1) / 5) for i in range(1, 5))}
    )
    for index in label_indexes:
        x = x_of(index)
        label = datetime.fromtimestamp(points[index][0], _MARKET_TZ).strftime("%m-%d")
        svg.append(
            f'<text x="{x:.2f}" y="{height - bottom + 34}" text-anchor="middle" fill="#8294ad" font-size="16">{label}</text>'
        )

    for index, (_, open_price, close_price, high_price, low_price) in enumerate(points):
        x = x_of(index)
        color = "#ef5350" if close_price >= open_price else "#26a69a"
        body_top = y_of(max(open_price, close_price))
        body_bottom = y_of(min(open_price, close_price))
        body_height = max(1.5, body_bottom - body_top)
        svg.append(
            f'<line x1="{x:.2f}" y1="{y_of(high_price):.2f}" x2="{x:.2f}" y2="{y_of(low_price):.2f}" stroke="{color}" stroke-width="2"/>'
        )
        svg.append(
            f'<rect x="{x - candle_width / 2:.2f}" y="{body_top:.2f}" width="{candle_width:.2f}" height="{body_height:.2f}" fill="{color}"/>'
        )

    ma7 = _polyline(points, 7, x_of, y_of)
    ma30 = _polyline(points, 30, x_of, y_of)
    svg.extend(
        (
            f'<polyline points="{ma7}" fill="none" stroke="#ffca5c" stroke-width="2.2" opacity="0.95"/>',
            f'<polyline points="{ma30}" fill="none" stroke="#62a8ff" stroke-width="2.2" opacity="0.95"/>',
            '<line x1="1080" y1="132" x2="1120" y2="132" stroke="#ffca5c" stroke-width="3"/><text x="1128" y="138" fill="#c8d5e8" font-size="17">7日均线</text>',
            '<line x1="1200" y1="132" x2="1240" y2="132" stroke="#62a8ff" stroke-width="3"/><text x="1248" y="138" fill="#c8d5e8" font-size="17">30日均线</text>',
            '<text x="720" y="950" text-anchor="middle" fill="#687b96" font-size="15">历史走势仅供数据观察，不代表价格预测</text>',
            "</svg>",
        )
    )
    return (
        "<!doctype html><html><body style='margin:0;background:#0b1220'>"
        + "".join(svg)
        + "</body></html>"
    )


async def render_kline(output: Path, data_file: Path) -> None:
    title, points = _load_payload(data_file)
    document = _chart_html(title, points)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            page = await context.new_page()
            await page.set_content(document, wait_until="domcontentloaded")
            png = await page.screenshot(type="png", animations="disabled")
            await asyncio.to_thread(output.write_bytes, png)
        finally:
            await browser.close()


def main(arguments: list[str] | None = None) -> int:
    try:
        output, data_file = _arguments(arguments or sys.argv)
        asyncio.run(render_kline(output, data_file))
    except BaseException as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
