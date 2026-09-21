"""PIL-rendered market snapshot cards from local sampled data.

Cards are built exclusively from persisted local stores (hourly snapshot
pool, daily index closes) and therefore never trigger a live market
request.  The colour language follows the fast K-line renderer: red for
rises, green for falls.
"""

from __future__ import annotations

import io
from datetime import date, datetime

from PIL import Image, ImageDraw

from .kline_fast_renderer import _font
from .market_report import ReportData

WIDTH = 1200
HEIGHT = 800
_UP_COLOR = "#ef5350"
_DOWN_COLOR = "#26a69a"
_TEXT_COLOR = "#f2f6ff"
_DIM_COLOR = "#8fa3bf"


def _new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#0b1220")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (24, 24, WIDTH - 24, HEIGHT - 24), radius=24, fill="#111a2b", outline="#263650"
    )
    return image, draw


def _encode(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=5)
    return output.getvalue()


def render_item_card(name: str, snapshot: dict[str, float]) -> bytes:
    """Render one item's local snapshot card (latest, 7-day average, change)."""

    image, draw = _new_canvas()
    draw.text((80, 72), name[:80], font=_font(38, bold=True), fill=_TEXT_COLOR)
    draw.text((80, 128), "本地样本池快照 · 非实时 · 红涨绿跌", font=_font(20), fill=_DIM_COLOR)

    latest = float(snapshot["latest_price"])
    average = float(snapshot["week_average"])
    change = float(snapshot["week_change"])
    samples = int(snapshot["samples"])
    data_time = datetime.fromtimestamp(float(snapshot["data_time"]))

    color = _UP_COLOR if change >= 0 else _DOWN_COLOR
    draw.text((80, 220), f"¥{latest:.2f}", font=_font(72, bold=True), fill=color)
    change_text = f"7日 {change:+.2f}%"
    change_box = draw.textbbox((0, 0), change_text, font=_font(40, bold=True))
    draw.text(
        (WIDTH - 100 - (change_box[2] - change_box[0]), 250),
        change_text,
        font=_font(40, bold=True),
        fill=color,
    )

    draw.line((80, 380, WIDTH - 80, 380), fill="#263650", width=2)
    rows = [
        ("7日样本均价", f"¥{average:.2f}"),
        ("样本数（7日）", f"{samples} 次"),
        ("最新样本时间", data_time.strftime("%Y-%m-%d %H:%M")),
    ]
    for index, (label, value) in enumerate(rows):
        y = 430 + index * 70
        draw.text((80, y), label, font=_font(24), fill=_DIM_COLOR)
        draw.text((420, y), value, font=_font(26), fill=_TEXT_COLOR)

    footer = "数据来源 SteamDT 公开行情，仅供参考 · 不构成交易建议"
    footer_box = draw.textbbox((0, 0), footer, font=_font(18))
    draw.text(
        ((WIDTH - (footer_box[2] - footer_box[0])) / 2, HEIGHT - 76),
        footer,
        font=_font(18),
        fill="#687b96",
    )
    return _encode(image)


def render_broad_card(closes: tuple[tuple[date, float], ...]) -> bytes:
    """Render the broad index card with a 30-day close sparkline."""

    image, draw = _new_canvas()
    draw.text((80, 72), "CS2 大盘指数", font=_font(38, bold=True), fill=_TEXT_COLOR)
    draw.text((80, 128), "本地日线快照 · 红涨绿跌", font=_font(20), fill=_DIM_COLOR)

    latest_date, latest_close = closes[-1]
    previous_close = closes[-2][1]
    change = (latest_close / previous_close - 1) * 100
    color = _UP_COLOR if change >= 0 else _DOWN_COLOR
    draw.text((80, 220), f"¥{latest_close:.2f}", font=_font(72, bold=True), fill=color)
    change_text = f"{change:+.2f}%"
    change_box = draw.textbbox((0, 0), change_text, font=_font(40, bold=True))
    draw.text(
        (WIDTH - 100 - (change_box[2] - change_box[0]), 250),
        change_text,
        font=_font(40, bold=True),
        fill=color,
    )

    selected = closes[-30:]
    low = min(close for _, close in selected)
    high = max(close for _, close in selected)
    left, right, top, bottom = 80, 80, 430, 640
    spread = max(high - low, 1e-9)
    chart_width = WIDTH - left - right
    points = [
        (
            left + chart_width * index / max(len(selected) - 1, 1),
            top + (high - close) / spread * (bottom - top),
        )
        for index, (_, close) in enumerate(selected)
    ]
    draw.line((left, top, WIDTH - right, top), fill="#263650", width=1)
    draw.line((left, bottom, WIDTH - right, bottom), fill="#263650", width=1)
    draw.line(points, fill=color, width=4, joint="curve")

    week = [close for _, close in closes[-7:]]
    stats = (
        ("截至", latest_date.isoformat()),
        ("近7日高/低", f"¥{max(week):.2f} / ¥{min(week):.2f}"),
        ("日线样本", f"{len(closes)} 天"),
    )
    for index, (label, value) in enumerate(stats):
        draw.text((80 + index * 360, 668), f"{label}  {value}", font=_font(22), fill=_DIM_COLOR)

    footer = "数据来源 SteamDT 公开行情，仅供参考 · 不构成交易建议"
    footer_box = draw.textbbox((0, 0), footer, font=_font(18))
    draw.text(
        ((WIDTH - (footer_box[2] - footer_box[0])) / 2, HEIGHT - 76),
        footer,
        font=_font(18),
        fill="#687b96",
    )
    return _encode(image)


def render_daily_card(data: ReportData) -> bytes:
    """Render the daily/weekly report as one PNG card with a 24h ranking.

    ``data`` is the structured report produced by
    :func:`src.services.market_report.build_report_data`.  Pure rendering —
    no network and no model.
    """

    weekly = bool(data["weekly"])
    today = data["today"]
    closes = data["closes"]
    risers = data["risers"]
    fallers = data["fallers"]
    alert_total = int(data["alert_total"])

    image, draw = _new_canvas()
    title = f"📅 塔菲{'周报' if weekly else '日报'} {today.strftime('%m-%d')}"
    draw.text((80, 60), title, font=_font(40, bold=True), fill=_TEXT_COLOR)
    draw.text((80, 116), "本地快照渲染 · 红涨绿跌", font=_font(20), fill=_DIM_COLOR)

    y = 180
    if len(closes) >= 2:
        latest_date, latest_close = closes[-1]
        previous_close = closes[-2][1]
        change = (latest_close / previous_close - 1) * 100
        color = _UP_COLOR if change >= 0 else _DOWN_COLOR
        draw.text(
            (80, y),
            f"大盘  ¥{latest_close:.2f}  {change:+.2f}%",
            font=_font(34, bold=True),
            fill=color,
        )
        draw.text(
            (80, y + 52),
            f"截至 {latest_date.isoformat()}",
            font=_font(20),
            fill=_DIM_COLOR,
        )
        y += 130
    else:
        draw.text((80, y), "大盘：日线数据积累中", font=_font(30), fill=_DIM_COLOR)
        y += 90

    draw.line((80, y, WIDTH - 80, y), fill="#263650", width=2)
    y += 30
    window = "24h" if not weekly else "7d"
    draw.text((80, y), f"🚀 涨跌排行（{window}）", font=_font(28, bold=True), fill=_TEXT_COLOR)
    y += 52

    def ranking_row(name: str, change: float, row_y: int, *, up: bool) -> int:
        color = _UP_COLOR if up else _DOWN_COLOR
        change_text = f"{change:+.1f}%"
        draw.text((80, row_y), change_text, font=_font(26, bold=True), fill=color)
        draw.text((240, row_y), name[:34], font=_font(24), fill=_TEXT_COLOR)
        return row_y + 44

    if risers:
        for name, change, _price, _time in risers:
            y = ranking_row(name, change, y, up=True)
    else:
        draw.text((80, y), "（上涨样本不足）", font=_font(22), fill=_DIM_COLOR)
        y += 44
    if fallers:
        for name, change, _price, _time in fallers:
            y = ranking_row(name, change, y, up=False)
    else:
        draw.text((80, y), "（下跌样本不足）", font=_font(22), fill=_DIM_COLOR)
        y += 44

    draw.line((80, y + 10, WIDTH - 80, y + 10), fill="#263650", width=2)
    draw.text(
        (80, y + 34),
        f"🔔 预警触发 {alert_total} 条" if alert_total else "🔔 预警触发：本时段无记录",
        font=_font(24),
        fill=_TEXT_COLOR,
    )

    footer = "数据来源 SteamDT 公开行情，仅供参考 · 不构成交易建议"
    footer_box = draw.textbbox((0, 0), footer, font=_font(18))
    draw.text(
        ((WIDTH - (footer_box[2] - footer_box[0])) / 2, HEIGHT - 76),
        footer,
        font=_font(18),
        fill="#687b96",
    )
    return _encode(image)


__all__ = ["render_broad_card", "render_daily_card", "render_item_card"]
