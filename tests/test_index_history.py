from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.services.index_history import IndexHistoryStore

_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def _payload(end_day: int, count: int, value: float = 100.0) -> dict:
    base = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    rows = [
        [int((base + timedelta(days=day - end_day)).timestamp()), value] for day in range(count)
    ]
    return {"success": True, "data": rows}


class IndexHistoryStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = int(datetime(2026, 8, 1, 12, 0, tzinfo=_TZ).timestamp())

    def _store(self, fetcher):
        return IndexHistoryStore(
            path=Path(self.tmp.name) / "history.db",
            fetcher=fetcher,
            wall_clock=lambda: self.now,
        )

    async def test_upsert_is_idempotent_and_refreshes_same_day(self) -> None:
        store = self._store(fetcher=None)  # type: ignore[arg-type]
        closes = {
            date(2026, 7, 30): 100.0,
            date(2026, 7, 31): 101.0,
            date(2026, 8, 1): 102.0,
        }
        await store.upsert_closes(closes)
        await store.upsert_closes({**closes, date(2026, 8, 1): 103.0})

        rows = await store.load_closes()
        # The current day (Aug 1) is not served; the two finalized past
        # days are, in date order.
        self.assertEqual(rows, ((date(2026, 7, 30), 100.0), (date(2026, 7, 31), 101.0)))
        # The same-day row itself was refreshed to 103 in the table.
        connection = sqlite3.connect(store.path)
        try:
            current = connection.execute(
                "SELECT close_value FROM index_daily_closes WHERE close_date = '2026-08-01'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(float(current[0]), 103.0)

    async def test_historical_closes_are_not_rewritten_by_later_backfills(self) -> None:
        store = self._store(fetcher=None)  # type: ignore[arg-type]
        first = {
            date(2026, 7, 30): 100.0,
            date(2026, 7, 31): 101.0,
            date(2026, 8, 1): 102.0,
        }
        revised = {
            date(2026, 7, 30): 999.0,
            date(2026, 7, 31): 999.0,
            date(2026, 8, 1): 103.0,
        }

        await store.upsert_closes(first)
        await store.upsert_closes(revised)

        # Past rows without a pending marker are frozen: the 999 rewrite
        # must not land.  The current day (Aug 1) is not served.
        self.assertEqual(
            await store.load_closes(),
            (
                (date(2026, 7, 30), 100.0),
                (date(2026, 7, 31), 101.0),
            ),
        )

    async def test_backfill_runs_once_when_current(self) -> None:
        calls = 0

        async def fetcher() -> dict:
            nonlocal calls
            calls += 1
            return _payload(end_day=0, count=3)

        store = self._store(fetcher=fetcher)
        self.assertEqual(await store.backfill_if_due(), 3)
        self.assertEqual(await store.backfill_if_due(), 0)
        self.assertEqual(calls, 1)

    async def test_failed_backfill_is_throttled(self) -> None:
        calls = 0

        async def fetcher() -> dict:
            nonlocal calls
            calls += 1
            raise ValueError("upstream unavailable")

        store = self._store(fetcher=fetcher)
        with self.assertRaises(ValueError):
            await store.backfill_if_due()
        # The recorded attempt throttles the next immediate call.
        self.assertEqual(await store.backfill_if_due(), 0)
        self.assertEqual(calls, 1)

    async def test_force_backfill_refreshes_even_when_current(self) -> None:
        calls = 0

        async def fetcher() -> dict:
            nonlocal calls
            calls += 1
            # Explicit current-date payload: Jul 31 (past) + Aug 1 (today),
            # so the test does not depend on a stale "July vs August" mix.
            base = datetime(2026, 8, 1, 12, 0, tzinfo=_TZ)
            value = 100.0 + calls
            return {
                "success": True,
                "data": [
                    [int((base - timedelta(days=1)).timestamp()), value - 1.0],
                    [int(base.timestamp()), value],
                ],
            }

        store = self._store(fetcher=fetcher)
        self.assertEqual(await store.backfill_if_due(force=True), 2)
        self.assertEqual(await store.backfill_if_due(force=True), 2)
        self.assertEqual(calls, 2)
        # force bypasses the throttle, so the request really executed twice;
        # the *current day* (Aug 1) row was refreshed to 102 in the table.
        connection = sqlite3.connect(store.path)
        try:
            current = connection.execute(
                "SELECT close_value FROM index_daily_closes WHERE close_date = '2026-08-01'"
            ).fetchone()
            older = connection.execute(
                "SELECT close_value FROM index_daily_closes WHERE close_date = '2026-07-31'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(float(current[0]), 102.0)
        # The past row (Jul 31) has no pending marker and stays frozen at
        # its first-seen value (100 from the first force run).
        self.assertEqual(float(older[0]), 100.0)


if __name__ == "__main__":
    unittest.main()
