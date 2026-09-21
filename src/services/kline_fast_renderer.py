"""Fast dependency-light PNG renderer for validated OHLC points.

Visual language v2 (weak-culture friendly, institutional dark):

* layered backgrounds — main chart deepest, sub-panels one step lighter,
  no hard borders between zones;
* semantic color — up/down bound to red/green everywhere (bodies, wicks in
  a darker sibling shade, change value, latest-price tag); KDJ overbought
  (>80) / oversold (<20) zones get faint red/green washes;
* information hierarchy — OHLC header cards (close amplified), latest
  price dashed line + colored tag, core indicator values big/bold while
  secondary ones shrink into gray, key time nodes only;
* guidance details — moving averages taper thick toward the latest bar,
  the latest candle gets a faint glow, MACD bars fade with distance from
  zero, swing high/low micro-dots, whisper-quiet grain over the backdrop.

All decoration is visual only: every plotted number is the exact computed
value — data is never smoothed, rescaled or rounded for looks.  The canvas
is painted at 2x and downsampled for anti-aliased edges.
"""

from __future__ import annotations

import io
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .indicators import wilder_rsi_series
from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

WIDTH = 1440
HEIGHT = 1320

#: Supersampling factor: paint everything at 2x, then LANCZOS downsample.
_SS = 2

# --- layout (2x supersampled coordinates; output is WIDTH x HEIGHT) ---
_MAIN_TOP = 470
_MAIN_BOTTOM = 1400
_PANEL_GAP = 80
_PANEL_HEIGHT = 440
_MACD_TOP = _MAIN_BOTTOM + _PANEL_GAP
_RSI_TOP = _MACD_TOP + _PANEL_HEIGHT + _PANEL_GAP
_KDJ_TOP = _RSI_TOP  # the bottom sub-chart is KDJ(9,3,3) since V0.5 night batch
_AXIS_LABEL_Y = _KDJ_TOP + _PANEL_HEIGHT + 44
_FOOTER_Y = HEIGHT * _SS - 64

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 14

# --- palette: layered dark, 红涨绿跌 bound everywhere ---
_BG_OUTER = "#0a101d"
_BG_MAIN = "#0c1424"
_BG_MACD = "#101a2e"
_BG_KDJ = "#121e36"
_GRID = "#22314d"
_TEXT = "#e8eefb"
_SUB = "#8fa3bf"
_DIM = "#8294ad"
_FAINT = "#5b6d89"
_WHISPER = "#42506b"
_UP = "#f23645"
_UP_WICK = "#a32430"
_DOWN = "#26a69a"
_DOWN_WICK = "#14655c"
_MA7 = "#ffb84d"
_MA30 = "#4da3ff"
_DIF = "#ff9f43"
_DEA = "#ffd166"
_K = "#b388ff"
_D = "#4da3ff"
_J = "#ff6b9d"


@lru_cache(maxsize=32)
def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Cache font objects per (size, bold); loading from disk is expensive.

    Rendered output depends only on the glyph outlines, so the cached objects
    are shared read-only across render threads (rendering itself is off the
    event loop via ``asyncio.to_thread`` at the call sites).
    """

    candidates = (
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path(
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
            if bold
            else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        ),
        Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _moving_average(points: list[list[float]], period: int) -> list[tuple[int, float]]:
    return [
        (
            index,
            sum(point[2] for point in points[index - period + 1 : index + 1]) / period,
        )
        for index in range(period - 1, len(points))
    ]


def _ema_series(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    series: list[float] = []
    for index, value in enumerate(values):
        series.append(value if index == 0 else alpha * value + (1 - alpha) * series[-1])
    return series


def _macd(
    points: list[list[float]],
    *,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (DIF, DEA, histogram) index-aligned series; Chinese 2x bar convention."""

    closes = [point[2] for point in points]
    empty: tuple[list[tuple[int, float]], list[tuple[int, float]], list[tuple[int, float]]] = (
        [],
        [],
        [],
    )
    if len(closes) < slow:
        return empty
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    dif_values = [
        (ema_fast[index] - ema_slow[index], index) for index in range(slow - 1, len(closes))
    ]
    if len(dif_values) < signal:
        return empty
    dea_values = _ema_series([value for value, _ in dif_values], signal)
    dif_series = [(index, value) for value, index in dif_values]
    dea_series = [(dif_values[offset][1], dea) for offset, dea in enumerate(dea_values)]
    histogram = [
        (index, (value - dea) * 2.0)
        for (index, value), (_, dea) in zip(dif_series, dea_series, strict=True)
    ]
    return dif_series, dea_series, histogram


def _rsi(points: list[list[float]], *, period: int = RSI_PERIOD) -> list[tuple[int, float]]:
    """RSI series over the candle closes, shared with the scoring service."""

    closes = [point[2] for point in points]
    return list(enumerate(wilder_rsi_series(closes, period=period), start=period))


def _kdj_series(points: list[list[float]]) -> list[tuple[int, tuple[float, float, float]]]:
    """Full (K, D, J) triples aligned to candle indexes (0-based, start=period).

    Uses the shared ``indicators.kdj_series`` so the chart and the scoring
    service see the same math.
    """

    from .indicators import kdj_series

    closes = [point[2] for point in points]
    triples = kdj_series(closes)
    start = len(closes) - len(triples)
    return list(enumerate(triples, start=start))


def _dotted_hline(
    draw: ImageDraw.ImageDraw,
    x0: float,
    x1: float,
    y: float,
    *,
    fill: str,
    width: int,
    dash: int = 16,
    gap: int = 14,
) -> None:
    """Soft dotted grid line (lightweight-charts style)."""

    x = x0
    while x < x1:
        draw.line((x, y, min(x + dash, x1), y), fill=fill, width=width)
        x += dash + gap


def _draw_panel_frame(
    draw: ImageDraw.ImageDraw,
    *,
    top: int,
    fill: str,
) -> None:
    """Borderless layered panel — separation comes from the tint step."""

    draw.rounded_rectangle(
        (200, top, WIDTH * _SS - 90, top + _PANEL_HEIGHT),
        radius=20,
        fill=fill,
    )


def render_kline_png(title: str, points: list[list[float]]) -> bytes:
    """Render already validated points; this function performs no network I/O."""

    if len(points) < 30:
        raise ValueError("K-line history is insufficient")
    selected = points[-90:]
    canvas = (WIDTH * _SS, HEIGHT * _SS)
    image = Image.new("RGB", canvas, _BG_OUTER)
    # Whisper-quiet grain over the backdrop (decor only, before any data ink).
    grain = Image.effect_noise(canvas, 14).convert("RGB")
    image = Image.blend(image, grain, 0.035)
    overlay = Image.new("RGBA", canvas, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (48, 48, WIDTH * _SS - 48, HEIGHT * _SS - 48),
        radius=48,
        fill=_BG_OUTER,
        outline=_GRID,
        width=2,
    )

    title_font = _font(34 * _SS, bold=True)
    body_font = _font(18 * _SS)
    label_font = _font(14 * _SS)
    value_font = _font(23 * _SS, bold=True)
    close_font = _font(29 * _SS, bold=True)
    small_font = _font(16 * _SS)
    change_font = _font(30 * _SS, bold=True)
    draw.text((200, 120), title[:100], font=title_font, fill=_TEXT)
    latest = selected[-1]
    change = (latest[2] / latest[1] - 1) * 100
    change_color = _UP if change >= 0 else _DOWN
    latest_date = datetime.fromtimestamp(latest[0], _MARKET_TZ).strftime("%Y-%m-%d")
    draw.text(
        (200, 216),
        f"真实日K · 快速本地绘制 · 红涨绿跌 · 数据日期 {latest_date}",
        font=body_font,
        fill=_SUB,
    )

    # OHLC header cards: small field label over a big value; close amplified
    # in the day's change color as the single focal number.
    cards = (
        ("开盘", f"{latest[1]:.2f}", _TEXT, value_font, 200),
        ("最高", f"{latest[3]:.2f}", _TEXT, value_font, 940),
        ("收盘", f"{latest[2]:.2f}", change_color, close_font, 1680),
        ("最低", f"{latest[4]:.2f}", _TEXT, value_font, 2420),
    )
    for label, value, color, font, x in cards:
        draw.text((x, 282), label, font=label_font, fill=_DIM)
        draw.text((x, 312), value, font=font, fill=color)

    change_text = f"{change:+.2f}%"
    change_box = draw.textbbox((0, 0), change_text, font=change_font)
    draw.text(
        (WIDTH * _SS - 120 - (change_box[2] - change_box[0]), 156),
        change_text,
        font=change_font,
        fill=change_color,
    )

    left, right, top, bottom = 200, 90, _MAIN_TOP, _MAIN_BOTTOM
    chart_width = WIDTH * _SS - left - right
    chart_height = bottom - top
    # Main zone gets its own deepest tint (plain backdrop block, no border).
    draw.rectangle((48, top - 40, WIDTH * _SS - 48, bottom + 40), fill=_BG_MAIN)
    low = min(point[4] for point in selected)
    high = max(point[3] for point in selected)
    padding = max((high - low) * 0.06, high * 0.002)
    low -= padding
    high += padding
    spread = max(high - low, 1e-9)
    step = chart_width / len(selected)
    candle_width = max(6, min(20, round(step * 0.62)))

    def x_of(index: int) -> float:
        return left + step * (index + 0.5)

    def y_of(price: float) -> float:
        return top + (high - price) / spread * chart_height

    for grid_index in range(7):
        ratio = grid_index / 6
        y = top + chart_height * ratio
        price = high - spread * ratio
        major = abs(price - round(price)) < spread * 0.01
        _dotted_hline(draw, left, WIDTH * _SS - right, y, fill=_GRID, width=3 if major else 2)
        label = f"{price:.2f}"
        box = draw.textbbox((0, 0), label, font=small_font)
        draw.text(
            (left - 28 - (box[2] - box[0]), y - 18),
            label,
            font=body_font if major else small_font,
            fill=_TEXT if major else _DIM,
        )

    # Moving averages first so candles stay on top (K线在上）: MA7 opaque
    # tapering thick toward the latest bar, MA30 thinner and translucent.
    ma7_series = _moving_average(selected, 7)
    for position in range(1, len(ma7_series)):
        fraction = position / max(1, len(ma7_series) - 1)
        (previous_index, previous_value), (mav_index, mav_value) = (
            ma7_series[position - 1],
            ma7_series[position],
        )
        draw.line(
            [(x_of(previous_index), y_of(previous_value)), (x_of(mav_index), y_of(mav_value))],
            fill=_MA7,
            width=round(5 + fraction * 4),
            joint="curve",
        )
    ma30_series = _moving_average(selected, 30)
    for position in range(1, len(ma30_series)):
        fraction = position / max(1, len(ma30_series) - 1)
        (previous_index, previous_value), (mav_index, mav_value) = (
            ma30_series[position - 1],
            ma30_series[position],
        )
        odraw.line(
            [(x_of(previous_index), y_of(previous_value)), (x_of(mav_index), y_of(mav_value))],
            fill=(77, 163, 255, 150),
            width=round(3 + fraction * 2),
            joint="curve",
        )

    for index, (_, open_price, close_price, high_price, low_price) in enumerate(selected):
        cx = x_of(index)
        rising = close_price >= open_price
        body_color = _UP if rising else _DOWN
        wick_color = _UP_WICK if rising else _DOWN_WICK
        draw.line((cx, y_of(high_price), cx, y_of(low_price)), fill=wick_color, width=4)
        body_top = y_of(max(open_price, close_price))
        body_bottom = y_of(min(open_price, close_price))
        if body_bottom - body_top < 4:
            body_bottom = body_top + 4
        draw.rounded_rectangle(
            (cx - candle_width / 2, body_top, cx + candle_width / 2, body_bottom),
            radius=3,
            fill=body_color,
        )
        # Faint top-edge sheen for a whisper of depth (decor only).
        odraw.line(
            (cx - candle_width / 2 + 4, body_top + 2, cx + candle_width / 2 - 4, body_top + 2),
            fill=(255, 255, 255, 26),
            width=2,
        )

    # Latest-candle glow so the eye lands on the current node.
    last_x = x_of(len(selected) - 1)
    glow_base = _UP if selected[-1][2] >= selected[-1][1] else _DOWN
    glow_rgb = tuple(int(glow_base[i : i + 2], 16) for i in (1, 3, 5))
    odraw.rounded_rectangle(
        (last_x - candle_width, top, last_x + candle_width, bottom),
        radius=12,
        fill=glow_rgb + (26,),
    )

    # Legend floats chipless in the lower-left calm water (line + text only).
    legend_y = bottom - 110
    draw.line((left + 28, legend_y + 20, left + 88, legend_y + 20), fill=_MA7, width=6)
    draw.text((left + 100, legend_y), "7日均线", font=small_font, fill=_TEXT)
    draw.line((left + 360, legend_y + 20, left + 420, legend_y + 20), fill=_MA30, width=6)
    draw.text((left + 432, legend_y), "30日均线", font=small_font, fill=_TEXT)

    # Latest-price anchor: a slightly heavier translucent solid line (never
    # dotted, so it cannot read as grid) plus a tag in the day's color.
    change_rgb = tuple(int(change_color[i : i + 2], 16) for i in (1, 3, 5))
    close_y = y_of(latest[2])
    tag_text = f"{latest[2]:.2f}"
    tag_box = draw.textbbox((0, 0), tag_text, font=small_font)
    tag_width = (tag_box[2] - tag_box[0]) + 44
    tag_height = 56
    tag_left = WIDTH * _SS - right - tag_width
    odraw.line((left, close_y, tag_left, close_y), fill=change_rgb + (215,), width=5)
    draw.rounded_rectangle(
        (tag_left, close_y - tag_height / 2, tag_left + tag_width, close_y + tag_height / 2),
        radius=12,
        fill=change_color,
    )
    draw.text(
        (tag_left + 22, close_y - 19),
        tag_text,
        font=small_font,
        fill="#ffffff",
    )

    # Swing high/low micro-dots (deterministic argmax/argmin over the window).
    high_index = max(range(len(selected)), key=lambda i: selected[i][3])
    low_index = min(range(len(selected)), key=lambda i: selected[i][4])
    for marker_index, marker_y, _ in (
        (high_index, y_of(selected[high_index][3]) - 26, True),
        (low_index, y_of(selected[low_index][4]) + 26, False),
    ):
        marker_x = x_of(marker_index)
        odraw.ellipse(
            (marker_x - 9, marker_y - 9, marker_x + 9, marker_y + 9),
            fill=(255, 184, 77, 220),
        )

    label_plan = _key_time_indexes(selected)
    kept: list[int] = []
    for index in label_plan:
        if all(abs(x_of(index) - x_of(done)) >= 230 for done in kept):
            kept.append(index)
    for index in sorted(kept):
        label = datetime.fromtimestamp(selected[index][0], _MARKET_TZ).strftime("%m-%d")
        box = draw.textbbox((0, 0), label, font=small_font)
        draw.text(
            (x_of(index) - (box[2] - box[0]) / 2, _AXIS_LABEL_Y),
            label,
            font=small_font,
            fill=_DIM,
        )

    _draw_macd_panel(draw, odraw, selected, x_of, step, small_font)
    _draw_kdj_panel(draw, odraw, selected, x_of, small_font)

    footer = "历史走势仅供数据观察，不代表价格预测"
    footer_font = _font(14 * _SS)
    footer_box = draw.textbbox((0, 0), footer, font=footer_font)
    draw.text(
        ((WIDTH * _SS - (footer_box[2] - footer_box[0])) / 2, _FOOTER_Y),
        footer,
        font=footer_font,
        fill=_WHISPER,
    )
    composed = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    composed = composed.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    composed.save(output, format="PNG", optimize=False, compress_level=5)
    return output.getvalue()


def _key_time_indexes(selected: list[list[float]]) -> list[int]:
    """Even time nodes in priority order: window ends, then month starts.

    Swing high/low keeps only its micro-dot on the chart, never a label,
    so the axis rhythm stays uniform.
    """

    last = len(selected) - 1
    ordered = [0, last]
    for index, point in enumerate(selected):
        day = datetime.fromtimestamp(point[0], _MARKET_TZ).day
        if day == 1 or day == 15:
            ordered.append(index)
    seen: list[int] = []
    for index in ordered:
        if index not in seen:
            seen.append(index)
    return seen


def _draw_macd_panel(
    draw: ImageDraw.ImageDraw,
    odraw: ImageDraw.ImageDraw,
    selected: list[list[float]],
    x_of,
    step: float,
    small_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    dif_series, dea_series, histogram = _macd(selected)
    plot_top = _MACD_TOP + 76
    plot_bottom = _MACD_TOP + _PANEL_HEIGHT - 28
    if not histogram:
        _draw_panel_frame(draw, top=_MACD_TOP, fill=_BG_MACD)
        _panel_title(draw, _MACD_TOP, "动能指标MACD(12,26,9)", "", small_font)
        draw.text(
            (232, (plot_top + plot_bottom) // 2 - 20),
            "样本不足，动能指标略",
            font=small_font,
            fill=_FAINT,
        )
        return
    latest_dif = dif_series[-1][1]
    latest_dea = dea_series[-1][1]
    latest_hist = histogram[-1][1]
    _draw_panel_frame(draw, top=_MACD_TOP, fill=_BG_MACD)
    value_font = _font(20 * _SS, bold=True)
    rest_font = _font(15 * _SS)
    _panel_title(
        draw, _MACD_TOP, f"动能指标MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})", "", small_font
    )
    core = f"动能柱 {latest_hist:+.2f}"
    core_box = draw.textbbox((0, 0), core, font=value_font)
    core_color = _UP if latest_hist >= 0 else _DOWN
    draw.text(
        (WIDTH * _SS - 232 - (core_box[2] - core_box[0]), _MACD_TOP + 14),
        core,
        font=value_font,
        fill=core_color,
    )
    rest = f"快线 {latest_dif:.2f}  慢线 {latest_dea:.2f}"
    rest_box = draw.textbbox((0, 0), rest, font=rest_font)
    draw.text(
        (
            WIDTH * _SS - 232 - (core_box[2] - core_box[0]) - 24 - (rest_box[2] - rest_box[0]),
            _MACD_TOP + 26,
        ),
        rest,
        font=rest_font,
        fill=_SUB,
    )
    max_absolute = max(
        max(abs(value) for _, value in histogram),
        max(abs(value) for _, value in dif_series),
        max(abs(value) for _, value in dea_series),
        1e-9,
    )
    plot_height = plot_bottom - plot_top
    middle = (plot_top + plot_bottom) / 2

    def y_of_indicator(value: float) -> float:
        return middle - value / max_absolute * (plot_height / 2)

    _dotted_hline(draw, 200, WIDTH * _SS - 90, middle, fill=_GRID, width=2)
    draw.text((232, middle - 18), "0", font=small_font, fill=_DIM)
    bar_width = max(4, min(12, round(step * 0.5)))
    for index, value in histogram:
        x = x_of(index)
        base = _UP if value >= 0 else _DOWN
        strength = min(1.0, abs(value) / max_absolute)
        alpha = round(60 + strength * 195)
        rgb = tuple(int(base[i : i + 2], 16) for i in (1, 3, 5))
        y = y_of_indicator(value)
        top_edge, bottom_edge = (min(y, middle), max(y, middle))
        if bottom_edge - top_edge < 3:
            bottom_edge = top_edge + 3
        odraw.rounded_rectangle(
            (x - bar_width / 2, top_edge, x + bar_width / 2, bottom_edge),
            radius=2,
            fill=rgb + (alpha,),
        )
    for series, color in ((dif_series, _DIF), (dea_series, _DEA)):
        draw.line(
            [(x_of(index), y_of_indicator(value)) for index, value in series],
            fill=color,
            width=4,
            joint="curve",
        )


def _panel_title(
    draw: ImageDraw.ImageDraw,
    top: int,
    title: str,
    note: str,
    small_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    draw.text((232, top + 20), title, font=small_font, fill=_TEXT)
    if note:
        draw.text(
            (WIDTH * _SS - 232 - draw.textlength(note, font=small_font), top + 20),
            note,
            font=small_font,
            fill=_TEXT,
        )


def _draw_kdj_panel(
    draw: ImageDraw.ImageDraw,
    odraw: ImageDraw.ImageDraw,
    selected: list[list[float]],
    x_of,
    small_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """KDJ(9,3,3) panel with an ATR(14) note — the bottom sub-chart.

    K/D are bounded 0..100; the overbought (>80) and oversold (<20) bands
    get faint semantic washes so the state reads without the numbers.  J is
    drawn on the same scale but may exceed it (clamped visually).  The ATR
    value is appended as a compact volatility readout.
    """

    from .indicators import atr

    kdj_series = _kdj_series(selected)
    plot_top = _KDJ_TOP + 76
    plot_bottom = _KDJ_TOP + _PANEL_HEIGHT - 28
    if not kdj_series:
        _draw_panel_frame(draw, top=_KDJ_TOP, fill=_BG_KDJ)
        _panel_title(draw, _KDJ_TOP, "随机指标KDJ(9,3,3)", "", small_font)
        draw.text(
            (232, (plot_top + plot_bottom) // 2 - 20),
            "样本不足，随机指标略",
            font=small_font,
            fill=_FAINT,
        )
        return
    latest = kdj_series[-1][1]
    atr_value = atr(
        [point[3] for point in selected],
        [point[4] for point in selected],
        [point[2] for point in selected],
    )
    _draw_panel_frame(draw, top=_KDJ_TOP, fill=_BG_KDJ)
    value_font = _font(20 * _SS, bold=True)
    rest_font = _font(15 * _SS)
    _panel_title(draw, _KDJ_TOP, "随机指标KDJ(9,3,3)", "", small_font)
    core = f"K值 {latest[0]:.1f}"
    core_box = draw.textbbox((0, 0), core, font=value_font)
    draw.text(
        (WIDTH * _SS - 232 - (core_box[2] - core_box[0]), _KDJ_TOP + 14),
        core,
        font=value_font,
        fill=_TEXT,
    )
    rest = f"D值 {latest[1]:.1f}  J值 {latest[2]:.1f}"
    if atr_value is not None:
        rest += f"  平均波幅 {atr_value:.2f}"
    rest_box = draw.textbbox((0, 0), rest, font=rest_font)
    draw.text(
        (
            WIDTH * _SS - 232 - (core_box[2] - core_box[0]) - 24 - (rest_box[2] - rest_box[0]),
            _KDJ_TOP + 26,
        ),
        rest,
        font=rest_font,
        fill=_SUB,
    )
    plot_height = plot_bottom - plot_top

    def y_of_kdj(value: float) -> float:
        return plot_bottom - max(0.0, min(100.0, value)) / 100.0 * plot_height

    # Semantic zone washes in unmistakable red/green (decor only;
    # thresholds stay the standard 80/20).
    odraw.rectangle(
        (200, y_of_kdj(100.0), WIDTH * _SS - 90, y_of_kdj(80.0)), fill=(255, 77, 90, 34)
    )
    odraw.rectangle((200, y_of_kdj(20.0), WIDTH * _SS - 90, y_of_kdj(0.0)), fill=(18, 224, 184, 34))
    for level, label in ((20.0, "20"), (50.0, "50"), (80.0, "80")):
        y = y_of_kdj(level)
        if level == 80.0:
            draw.line((200, y, WIDTH * _SS - 90, y), fill="#e05663", width=4)
        elif level == 20.0:
            draw.line((200, y, WIDTH * _SS - 90, y), fill="#2fa89c", width=4)
        else:
            _dotted_hline(draw, 200, WIDTH * _SS - 90, y, fill=_GRID, width=2)
        draw.text((232, y - 18), label, font=small_font, fill=_DIM)
    k_line = [(x_of(index), y_of_kdj(triple[0])) for index, triple in kdj_series]
    d_line = [(x_of(index), y_of_kdj(triple[1])) for index, triple in kdj_series]
    j_line = [(x_of(index), y_of_kdj(triple[2])) for index, triple in kdj_series]
    draw.line(k_line, fill=_K, width=4, joint="curve")
    draw.line(d_line, fill=_D, width=4, joint="curve")
    draw.line(j_line, fill=_J, width=4, joint="curve")


__all__ = ["MACD_FAST", "MACD_SIGNAL", "MACD_SLOW", "RSI_PERIOD", "render_kline_png"]
