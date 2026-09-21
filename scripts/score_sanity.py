"""Offline sanity run for the V0.4 trade-score rules (not part of the bot).

Loads the real market_history database read-only, scores the most-sampled
items through the exact production code path and fails on any non-finite
number, ZeroDivisionError or missing disclaimer.  Run manually after
changing scoring rules:

    uv run --frozen python -m scripts.score_sanity
"""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.market_scoring import (  # noqa: E402
    MIN_DAILY_SAMPLES,
    compute_trade_score,
    render_trade_score,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_HISTORY_DB = PROJECT_ROOT / "data" / "market_history.db"


def _load_items(limit: int = 20) -> list[tuple[str, list[float], int | None, int | None]]:
    if not _HISTORY_DB.is_file():
        print(f"market_history.db not found at {_HISTORY_DB}; nothing to sanity-check")
        return []
    connection = sqlite3.connect(f"file:{_HISTORY_DB}?mode=ro", uri=True)
    try:
        names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT market_name, COUNT(*) AS buckets
                FROM quote_snapshots GROUP BY market_name
                ORDER BY buckets DESC LIMIT ?
                """,
                (limit,),
            )
        ]
        items: list[tuple[str, list[float], int | None, int | None]] = []
        for name in names:
            daily: dict[int, float] = {}
            counts: tuple[int | None, int | None] | None = None
            for bucket_at, sell_price, sell_count, bid_count in connection.execute(
                """
                SELECT bucket_at, sell_price, sell_count, bid_count
                FROM quote_snapshots WHERE market_name = ?
                ORDER BY bucket_at ASC
                """,
                (name,),
            ):
                price = float(sell_price)
                if price > 0:
                    daily[(int(bucket_at) + 8 * 3600) // 86400] = price
                counts = (
                    int(sell_count) if sell_count is not None else None,
                    int(bid_count) if bid_count is not None else None,
                )
            closes = [daily[day] for day in sorted(daily)]
            items.append(
                (name, closes, counts[0] if counts else None, counts[1] if counts else None)
            )
        return items
    finally:
        connection.close()


async def _main() -> int:
    items = _load_items()
    scored = 0
    skipped = 0
    for name, closes, sell_count, bid_count in items:
        if len(closes) < MIN_DAILY_SAMPLES:
            skipped += 1
            continue
        score = compute_trade_score(
            closes,
            sample_days=len(closes),
            sell_count=sell_count,
            bid_count=bid_count,
            spread_ratio=None,
        )
        if score is None:
            skipped += 1
            continue
        for side in (score.buy, score.sell):
            assert math.isfinite(side.total), f"{name}: non-finite total"
            for factor in side.factors:
                assert math.isfinite(factor.score), f"{name}/{factor.name}: non-finite score"
                assert 0 <= factor.score <= 100, f"{name}/{factor.name}: out of range"
                assert factor.weight > 0, f"{name}/{factor.name}: non-positive weight"
        text = render_trade_score(name, score)
        assert "不构成交易建议" in text, f"{name}: disclaimer missing"
        scored += 1
        print(
            f"ok {name}: buy {score.buy.total:.0f} / sell {score.sell.total:.0f} ({score.sample_days}d)"
        )
    print(f"sanity done: scored={scored} skipped={skipped} (below gate)")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("TAFFY_STATE_DB", str(PROJECT_ROOT / "data" / "taffy.db"))
    raise SystemExit(asyncio.run(_main()))
