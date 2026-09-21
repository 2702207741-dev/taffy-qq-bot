"""Batch 6: broad daily-index lifecycle — intraday update, cross-day
finalize, history freeze.

Contract (METHODOLOGY §1, 2026-09-06):

* Current-day rows may be inserted and refreshed, and must carry a
  ``pending_close:<date>`` marker in ``index_collection_state``.
* A past date with no stored row is first inserted as a local final value.
* A past date that carries a pending marker is updated once when it appears
  in a later response, and the marker is deleted (finalized).
* A past date with a stored row and no pending marker keeps its close
  value forever — including truncated responses and force=True.
* A pending date missing from a response keeps its value and marker; time
  passing alone must not finalize it.
* Future dates raise and nothing is partially written.

Each test runs the real store methods against its own temporary SQLite
file; every store instance carries an injected UTC+8 wall clock, and the
upserts/backfills below run without an explicit ``now`` so they classify
by that injected observation time (the cross-midnight case uses two
instances whose clocks straddle the boundary — the request-start clock
must govern).  Only fetchers and the transaction-failure boundary are
stubbed.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.services import index_history as index_history_module
from src.services.index_history import IndexHistoryStore

_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

D5 = date(2026, 9, 5)
D6 = date(2026, 9, 6)
D7 = date(2026, 9, 7)
D8 = date(2026, 9, 8)
D30 = date(2026, 9, 30)
D_OCT1 = date(2026, 10, 1)
LEGACY = date(2026, 8, 20)


def _ts(y: int, m: int, d: int, h: int = 12, minute: int = 0) -> int:
    return int(datetime(y, m, d, h, minute, tzinfo=_TZ).timestamp())


# UTC+8 wall-clock anchors.
SEP_05_1000 = _ts(2026, 9, 5, 10, 0)
SEP_05_1100 = _ts(2026, 9, 5, 11, 0)
SEP_06_1000 = _ts(2026, 9, 6, 10, 0)
SEP_07_1000 = _ts(2026, 9, 7, 10, 0)
SEP_08_1000 = _ts(2026, 9, 8, 10, 0)
SEP_30_2200 = _ts(2026, 9, 30, 22, 0)
SEP_30_2359 = _ts(2026, 9, 30, 23, 59)
OCT_01_0001 = _ts(2026, 10, 1, 0, 1)


def _store(tmp_path: Path, clock: float) -> IndexHistoryStore:
    return IndexHistoryStore(path=tmp_path / "history.db", wall_clock=lambda: clock)


class _Clock:
    """Mutable injected wall clock (seconds since epoch)."""

    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, to: float) -> None:
        self.value = to


async def _open(tmp_path: Path, clock: float) -> IndexHistoryStore:
    store = _store(tmp_path, clock)
    await store.initialize()
    return store


def _state(store: IndexHistoryStore) -> dict[str, str]:
    connection = sqlite3.connect(store.path)
    try:
        rows = connection.execute("SELECT key, value FROM index_collection_state").fetchall()
    finally:
        connection.close()
    return {str(key): str(value) for key, value in rows}


def _pending(store: IndexHistoryStore) -> set[str]:
    return {key for key in _state(store) if key.startswith("pending_close:")}


def _closes(store: IndexHistoryStore) -> dict[date, float]:
    connection = sqlite3.connect(store.path)
    try:
        rows = connection.execute(
            "SELECT close_date, close_value FROM index_daily_closes"
        ).fetchall()
    finally:
        connection.close()
    return {date.fromisoformat(str(d)): float(v) for d, v in rows}


async def _plant_raw(store: IndexHistoryStore, day: date, value: float, fetched_at: int) -> None:
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO index_daily_closes"
            " (close_date, close_value, source, fetched_at) VALUES (?, ?, ?, ?)",
            (day.isoformat(), value, "legacy", fetched_at),
        )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 1. Same-day continues to refresh (pre-fix: never refreshes after first hit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_day_second_hour_refreshes_in_place(tmp_path: Path) -> None:
    """Write 100 at 10:00 on Sep 5; a same-day 11:00 backfill requests again
    and updates the row to 110; the current day is not in load_closes()."""
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})
    assert _closes(first)[D5] == 100.0
    assert _pending(first) == {"pending_close:2026-09-05"}

    calls = 0

    async def fetcher() -> dict:
        nonlocal calls
        calls += 1
        return {"success": True, "data": [[_ts(2026, 9, 5, 8, 0), 110.0]]}

    later = await _open(tmp_path, SEP_05_1100)
    later._fetcher = fetcher  # type: ignore[assignment]
    # The one-hour throttle has elapsed; a normal backfill must fetch.
    assert await later.backfill_if_due() == 1
    assert calls == 1
    assert _closes(later)[D5] == 110.0
    assert _pending(later) == {"pending_close:2026-09-05"}
    assert (await later.load_closes()) == ()


# ---------------------------------------------------------------------------
# 2. Cross-day finalize (pre-fix: yesterday's intraday value never updates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_day_response_finalizes_yesterday(tmp_path: Path) -> None:
    """On Sep 6 the upstream returns Sep 5=115 and Sep 6=120: Sep 5 is
    finalized to 115 and readable; Sep 6 stays pending."""
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})

    next_day = await _open(tmp_path, SEP_06_1000)
    await next_day.upsert_closes({D5: 115.0, D6: 120.0})
    rows = _closes(next_day)
    assert rows[D5] == 115.0
    assert rows[D6] == 120.0
    assert _pending(next_day) == {"pending_close:2026-09-06"}
    assert (await next_day.load_closes()) == ((D5, 115.0),)


# ---------------------------------------------------------------------------
# 3. Finalized rows resist later rewrites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalized_history_resists_rewrite(tmp_path: Path) -> None:
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})
    next_day = await _open(tmp_path, SEP_06_1000)
    await next_day.upsert_closes({D5: 115.0, D6: 120.0})  # Sep 5 finalized

    later = await _open(tmp_path, SEP_07_1000)
    await later.upsert_closes({D5: 999.0})  # upstream "correction"
    assert _closes(later)[D5] == 115.0
    assert (await later.load_closes()) == ((D5, 115.0),)


# ---------------------------------------------------------------------------
# 4. Truncated response cannot rewrite frozen history
#    (pre-fix: the only returned old date becomes "newest" and is overwritten)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_response_cannot_rewrite_frozen(tmp_path: Path) -> None:
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})
    next_day = await _open(tmp_path, SEP_06_1000)
    await next_day.upsert_closes({D5: 115.0, D6: 120.0})
    later = await _open(tmp_path, SEP_07_1000)
    await later.upsert_closes({D7: 130.0})
    # D5 finalized on Sep 6; D6 (written Sep 6) and D7 (written Sep 7)
    # never received a finalizing later response -> still pending.

    truncated = await _open(tmp_path, SEP_08_1000)
    await truncated.upsert_closes({D5: 999.0})  # only an old frozen date
    rows = _closes(truncated)
    assert rows[D5] == 115.0
    assert rows[D6] == 120.0
    assert rows[D7] == 130.0
    # D6 never received a finalizing response (still pending) and D7 is
    # the current day: neither is served by load_closes().
    assert (await truncated.load_closes()) == ((D5, 115.0),)


# ---------------------------------------------------------------------------
# 5. Restart keeps pending/finalized state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_preserves_state(tmp_path: Path) -> None:
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})
    next_day = await _open(tmp_path, SEP_06_1000)
    await next_day.upsert_closes({D5: 115.0, D6: 120.0})

    # Reopen the same db file with a fresh instance at the same clock.
    reopened = await _open(tmp_path, SEP_06_1000)
    assert _pending(reopened) == {"pending_close:2026-09-06"}
    assert (await reopened.load_closes()) == ((D5, 115.0),)

    later = await _open(tmp_path, SEP_07_1000)
    await later.upsert_closes({D5: 999.0})
    assert _closes(later)[D5] == 115.0


# ---------------------------------------------------------------------------
# 6. A pending date missing from a response is not silently finalized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_date_missing_from_response_keeps_marker(tmp_path: Path) -> None:
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})  # D5 pending

    # Sep 6 response only contains Sep 6; Sep 5 absent -> Sep 5 keeps its
    # value and marker, Sep 6 is the current day (its own marker).
    next_day = await _open(tmp_path, SEP_06_1000)
    await next_day.upsert_closes({D6: 120.0})
    assert _pending(next_day) == {
        "pending_close:2026-09-05",
        "pending_close:2026-09-06",
    }
    assert _closes(next_day)[D5] == 100.0
    assert (await next_day.load_closes()) == ()

    # A later response that finally contains Sep 5 finalizes it (Sep 5 and
    # Sep 6 are both past by Sep 7, each with a pending marker).
    later = await _open(tmp_path, SEP_07_1000)
    await later.upsert_closes({D5: 115.0, D6: 120.0})
    assert _pending(later) == set()
    assert (await later.load_closes()) == ((D5, 115.0), (D6, 120.0))


# ---------------------------------------------------------------------------
# 7. Read boundary: only finalized history is served
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_closes_serves_only_finalized_history(tmp_path: Path) -> None:
    # D5 finalized, D6 current/pending, plus a future (Sep 9) raw legacy
    # row that must never be served.
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})
    next_day = await _open(tmp_path, SEP_06_1000)
    await next_day.upsert_closes({D5: 115.0, D6: 120.0})
    await _plant_raw(next_day, date(2026, 9, 9), 999.0, SEP_06_1000)

    loaded = await next_day.load_closes()
    assert loaded == ((D5, 115.0),)
    # Ordering/type contract: tuple of (date, float).
    assert all(isinstance(d, date) and isinstance(v, float) for d, v in loaded)


# ---------------------------------------------------------------------------
# 8. Failure throttles; force cannot rewrite frozen history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_throttles_and_force_keeps_frozen(tmp_path: Path) -> None:
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})
    next_day = await _open(tmp_path, SEP_06_1000)
    await next_day.upsert_closes({D5: 115.0, D6: 120.0})  # D5 finalized

    calls = 0

    async def failing() -> dict:
        nonlocal calls
        calls += 1
        raise ValueError("upstream unavailable")

    later = await _open(tmp_path, SEP_07_1000)
    later._fetcher = failing  # type: ignore[assignment]
    with pytest.raises(ValueError):
        await later.backfill_if_due(force=True)
    # The failed attempt throttles the next immediate call.
    assert await later.backfill_if_due() == 0
    assert calls == 1

    # A direct upsert (what a force refresh would deliver) cannot rewrite
    # the frozen D5.
    await later.upsert_closes({D5: 999.0})
    assert _closes(later)[D5] == 115.0


# ---------------------------------------------------------------------------
# 9. Cross-midnight: the request-start clock governs classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_midnight_request_stays_conservative(tmp_path: Path) -> None:
    """A backfill request that started Sep 30 23:59:59 and whose response
    arrives after midnight (Oct 1 00:00:01) must classify by the request
    start time captured before the fetch: Sep 30 updates its intraday row
    and stays pending; only a request *started* on Oct 1 finalizes it.

    Driven through the real backfill_if_due with a mutable injected clock
    — the synthetic fetcher advances the clock past midnight before
    returning, exactly like a slow upstream response."""
    # One store, one mutable clock: the real request path.
    clock = _Clock(SEP_30_2200)
    store = IndexHistoryStore(
        path=tmp_path / "history.db",
        wall_clock=clock,
        fetcher=None,  # type: ignore[arg-type]
    )
    await store.initialize()
    await store.upsert_closes({D30: 100.0})  # Sep 30 22:00 intraday

    responses = [  # (advance clock to, payload last date, values)
        (OCT_01_0001, [(D30, 110.0)]),
    ]
    calls = 0

    async def fetcher() -> dict:
        nonlocal calls
        target, rows = responses[calls]
        calls += 1
        clock.advance(target)  # the response arrives after midnight
        return {
            "success": True,
            "data": [[_ts(d.year, d.month, d.day, 8, 0), v] for d, v in rows],
        }

    store._fetcher = fetcher  # type: ignore[assignment]
    # Clock advances to 23:59:59; the request starts now (Sep 30) and the
    # fetcher then moves the clock to Oct 1 00:00:01 before returning.
    clock.advance(SEP_30_2359)
    assert await store.backfill_if_due() == 1
    # Sep 30 was refreshed to 110 but must still be pending (the request
    # started on Sep 30, so this is an intraday update, not a finalize).
    assert _closes(store)[D30] == 110.0
    assert _pending(store) == {"pending_close:2026-09-30"}
    assert (await store.load_closes()) == ()

    # A request started on Oct 1 (clock already there) finalizes Sep 30.
    responses.append((OCT_01_0001, [(D30, 115.0), (D_OCT1, 120.0)]))
    clock.advance(OCT_01_0001 + 3600)  # Oct 1 01:00:01
    assert await store.backfill_if_due() == 2
    assert _closes(store)[D30] == 115.0
    assert _closes(store)[D_OCT1] == 120.0
    assert _pending(store) == {"pending_close:2026-10-01"}
    assert (await store.load_closes()) == ((D30, 115.0),)


# ---------------------------------------------------------------------------
# 10. Atomicity: a commit failure rolls back value and marker together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_value_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sep 5 10:00 writes the current-day row and its pending marker.
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})
    assert _pending(first) == {"pending_close:2026-09-05"}

    # A Sep 6 instance is created AND initialized first, so the failing
    # connection factory is only installed for the write under test.
    next_day = await _open(tmp_path, SEP_06_1000)

    class Boom(RuntimeError):
        pass

    class FailingConnection(sqlite3.Connection):
        def commit(self) -> None:  # type: ignore[override]
            raise Boom("injected commit failure")

    def failing_connect(path: Path, *, timeout_seconds: float, label: str) -> sqlite3.Connection:
        del label
        connection = FailingConnection(path, timeout=timeout_seconds)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(index_history_module, "connect_local_database", failing_connect)
    with pytest.raises(Boom, match="injected commit failure"):
        # The transaction really executes "delete Sep 5 marker + add Sep 6
        # marker + write both rows" before the commit fails.
        await next_day.upsert_closes({D5: 115.0, D6: 120.0})
    monkeypatch.undo()

    # Reopen with the real factory and verify full rollback:
    rows = _closes(next_day)
    assert rows[D5] == 100.0  # yesterday unchanged
    assert D6 not in rows  # today's row absent
    assert _pending(next_day) == {"pending_close:2026-09-05"}  # yesterday's marker back
    assert "pending_close:2026-09-06" not in _pending(next_day)  # today's marker gone
    assert (await next_day.load_closes()) == ()  # nothing finalized
    # The original exception already propagated via pytest.raises above.


# ---------------------------------------------------------------------------
# 11. Future dates reject the whole batch atomically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_future_date_rejects_whole_batch(tmp_path: Path) -> None:
    first = await _open(tmp_path, SEP_05_1000)
    await first.upsert_closes({D5: 100.0})

    future = date(2026, 9, 30)  # beyond the Sep 5 observation clock
    with pytest.raises(ValueError, match="future"):
        await first.upsert_closes({D5: 115.0, future: 120.0})
    rows = _closes(first)
    assert rows[D5] == 100.0
    assert future not in rows
    assert _pending(first) == {"pending_close:2026-09-05"}


# ---------------------------------------------------------------------------
# 12. Legacy unmarked history rows stay frozen (compatibility)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_unmarked_history_stays_frozen(tmp_path: Path) -> None:
    first = await _open(tmp_path, SEP_05_1000)
    await _plant_raw(first, LEGACY, 50.0, _ts(2026, 8, 20))
    await first.upsert_closes({D5: 100.0})

    later = await _open(tmp_path, SEP_06_1000)
    await later.upsert_closes({LEGACY: 999.0, D5: 115.0})
    rows = _closes(later)
    assert rows[LEGACY] == 50.0  # frozen by compatibility, never rewritten
    assert rows[D5] == 115.0
    assert _pending(later) == set()  # nothing left pending


# ---------------------------------------------------------------------------
# 13. Backfill throttle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_throttled_within_hour(tmp_path: Path) -> None:
    calls = 0

    async def fetcher() -> dict:
        nonlocal calls
        calls += 1
        return {"success": True, "data": [[_ts(2026, 9, 5, 8, 0), 100.0]]}

    first = await _open(tmp_path, SEP_05_1000)
    first._fetcher = fetcher  # type: ignore[assignment]
    assert await first.backfill_if_due() == 1
    assert await first.backfill_if_due() == 0  # within the hour
    assert calls == 1

    calls2 = 0

    async def fetcher2() -> dict:
        nonlocal calls2
        calls2 += 1
        return {"success": True, "data": [[_ts(2026, 9, 5, 9, 0), 110.0]]}

    later = await _open(tmp_path, SEP_05_1100)
    later._fetcher = fetcher2  # type: ignore[assignment]
    assert await later.backfill_if_due() == 1  # hour elapsed -> fetch again
    assert calls2 == 1
    assert _closes(later)[D5] == 110.0
