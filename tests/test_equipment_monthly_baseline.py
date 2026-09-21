"""Batch 4: the equipment-index component baseline is fixed for its month.

The component refresh lifecycle (batch-4 contract, METHODOLOGY §8):

* No components yet -> build from the current candidate rules; fewer than
  200 components is honest accumulation progress, never a published index.
* Existing components below 200 -> a later refresh may rebuild, so the
  first small sample does not freeze the month.
* At least 200 components that all belong to the **current UTC+8 calendar
  month** -> the baseline is frozen for the month: names, base_day,
  base_price and added_at must not change, and the refresh returns the
  existing actual component count.
* A complete baseline crossing into a new month -> compute the new
  candidates first; when at least 200 valid components exist, atomically
  replace the whole baseline.
* New-month candidates below 200 or empty -> keep the old baseline and old
  base date (never fake a new-month base); return the retained count.
* An exception during a rebuild -> roll back; the old baseline stays fully
  intact and the exception keeps propagating to the caller.

Each case runs against its own temporary MarketHistoryStore under the
controlled test entry, writes synthetic quotes only, and drives the real
refresh function plus EquipmentIndexService.status() — no mocked SQL
results and no mocked index.

All clocks are explicit UTC+8 wall times.  The cross-month pair is chosen
so that the two instants are still the **same day in UTC** (2026-09-30
23:30+08:00 vs 2026-10-01 00:30+08:00) — only a UTC+8 month comparison
sees the boundary, so a test that accidentally used local/UTC time cannot
pass.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.services.equipment_index import EquipmentIndexService
from src.services.market_history import MarketHistoryStore

# --- fixed UTC+8 wall-clock anchors -----------------------------------------
# The existing test suite pins NOW = 1_800_000_000; the "current month"
# cases reuse that era (2027-01) so hour arithmetic stays inside one
# UTC+8 month.
_JAN_16_00 = 1_800_000_000  # 2027-01-15 16:00 +08:00
_JAN_17_00 = _JAN_16_00 + 3600

# Cross-month pair: both instants are 2026-09-30 in UTC but fall on
# different UTC+8 calendar days, so the tests only pass when the month
# boundary is judged in UTC+8.
_SEP_30_2330 = 1_790_782_200  # 2026-09-30 23:30 +08:00 (UTC 15:30)
_OCT_01_0030 = 1_790_785_800  # 2026-10-01 00:30 +08:00 (UTC 16:30)

# Candidate scan covers the trailing 30 days ending at the refresh clock.
# A baseline built on 2026-09-05 is outside the window of a refresh on
# 2026-10-15 (window starts 2026-09-15), so old quotes can never help a
# thin new month reach the candidate count.
_SEP_05_1200 = 1_788_580_800  # 2026-09-05 12:00 +08:00
_OCT_15_1200 = 1_792_036_800  # 2026-10-15 12:00 +08:00

# Hour-aligned buckets (quote rows carry bucket_at = hour boundary).
_SEP30_23_BUCKET = 1_790_780_400  # 2026-09-30 23:00 +08:00
_OCT1_00_BUCKET = 1_790_784_000  # 2026-10-01 00:00 +08:00

#: The refresh builds the full Top-250 list; the readiness gate still
#: requires the current bucket to cover at least 200 of them.
_TARGET = 250
_MIN = 200

_P0 = 100.0
_P1 = 110.0


def _assert_utc8(epoch: int, expect: str) -> None:
    got = (datetime.fromtimestamp(epoch, timezone.utc) + timedelta(hours=8)).strftime(
        "%Y-%m-%d %H:%M"
    )
    assert got == expect, f"{epoch} should be {expect} +08:00, got {got}"


def _insert_snapshot(
    store: MarketHistoryStore,
    name: str,
    bucket_at: int,
    price: float,
) -> None:
    """Insert one validated quote row directly into the temp db (synthetic
    market data on the test's own connection)."""
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO quote_snapshots
                (bucket_at, market_name, sell_price, bid_price,
                 sell_count, bid_count, data_time)
            VALUES (?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (bucket_at, name, price, bucket_at),
        )
        connection.commit()
    finally:
        connection.close()


def _read_components(store: MarketHistoryStore) -> list[tuple[str, str, float, int]]:
    """Full sorted component rows ``(name, base_day, base_price, added_at)``."""
    connection = sqlite3.connect(store.path)
    try:
        rows = connection.execute(
            "SELECT market_name, base_day, base_price, added_at FROM equipment_index_components"
        ).fetchall()
    finally:
        connection.close()
    return sorted((str(row[0]), str(row[1]), float(row[2]), int(row[3])) for row in rows)


@pytest.fixture
async def store(tmp_path: Path) -> MarketHistoryStore:
    s = MarketHistoryStore(path=tmp_path / "history.db", wall_clock=lambda: _JAN_16_00)
    await s.initialize()  # creates the quote/component tables (idempotent)
    return s


async def _seed_and_refresh(
    store: MarketHistoryStore,
    *,
    count: int,
    price: float,
    clock: int,
    prefix: str,
) -> list[str]:
    """Insert ``count`` items priced in the top hour and the hour before,
    run one real refresh at ``clock``, and return the component names."""
    bucket = clock // 3600 * 3600
    names = [f"{prefix}-{index:03d}" for index in range(count)]
    for name in names:
        _insert_snapshot(store, name, bucket, price)
        _insert_snapshot(store, name, bucket - 3600, price)
    result = await store.refresh_equipment_components(now=clock)
    assert result == count, f"seed refresh returned {result}, expected {count}"
    return names


async def _refresh(store: MarketHistoryStore, *, clock: int, expected: int) -> int:
    result = await store.refresh_equipment_components(now=clock)
    rows = _read_components(store)
    assert result == len(rows) == expected, (
        f"refresh returned {result}, table has {len(rows)}, expected {expected}"
    )
    return result


@pytest.mark.asyncio
async def test_accumulation_grows_10_to_100_to_250_same_month(
    store: MarketHistoryStore,
) -> None:
    """Accumulation: 10 -> 100 -> 250 in the same month.  Each step checks
    the return value, the actual table row count and status.components; the
    first two steps must not publish, 250 with full coverage may."""
    # Step clocks: each refresh sees a newer top hour so the latest bucket
    # always covers the items seeded up to that step.
    clocks = [_JAN_16_00, _JAN_17_00, _JAN_17_00 + 3600]

    def seed_to(n: int, clock: int) -> None:
        b = clock // 3600 * 3600
        for index in range(n):
            _insert_snapshot(store, f"grow-{index:03d}", b, _P0)
            _insert_snapshot(store, f"grow-{index:03d}", b - 3600, _P0)

    async def assert_state(step: int, expected: int, publishable: bool) -> None:
        rows = _read_components(store)
        assert len(rows) == expected, f"step {step}: table has {len(rows)}, expected {expected}"
        status = await EquipmentIndexService(store).status()
        assert status.components == expected, f"step {step}: status.components wrong"
        assert status.ready is publishable, (
            f"step {step}: ready={status.ready}, expected {publishable}"
        )

    # Step 1: 10 candidates -> established as accumulation progress.
    seed_to(10, clocks[0])
    assert await store.refresh_equipment_components(now=clocks[0]) == 10
    await assert_state(1, 10, publishable=False)

    # Step 2: 100 candidates -> rebuild (not frozen), table grows.
    seed_to(100, clocks[1])
    assert await store.refresh_equipment_components(now=clocks[1]) == 100
    await assert_state(2, 100, publishable=False)

    # Step 3: 250 candidates, all covered -> full baseline, publishable.
    seed_to(_TARGET, clocks[2])
    assert await store.refresh_equipment_components(now=clocks[2]) == _TARGET
    await assert_state(3, _TARGET, publishable=True)

    status = await EquipmentIndexService(store).status()
    assert status.ready is True
    assert status.latest_value == pytest.approx(_P0, abs=1e-6)


@pytest.mark.asyncio
async def test_same_month_freeze_keeps_columns_and_index_value(
    store: MarketHistoryStore,
) -> None:
    """A frozen baseline must ignore a same-month price move (index stays 100).

    Before the fix the refresh rewrote base_price to the latest 110 so the
    index read ~110.  After the fix the four baseline columns are unchanged
    and the real index stays ~100 (not 110).
    """
    bucket = _JAN_16_00 // 3600 * 3600
    names = await _seed_and_refresh(
        store, count=_TARGET, price=_P0, clock=_JAN_16_00, prefix="base"
    )
    before = _read_components(store)
    assert len(before) == _TARGET
    assert before[0][1].startswith("2027-01")  # base_day in the UTC+8 month

    # Same month, next hour: every item now trades at 110.
    later_bucket = bucket + 3600
    for name in names:
        _insert_snapshot(store, name, later_bucket, _P1)

    await store.refresh_equipment_components(now=_JAN_17_00)
    assert _read_components(store) == before, "frozen baseline changed in the same month"

    status = await EquipmentIndexService(store).status()
    assert status.ready is True
    assert status.latest_value == pytest.approx(_P1, abs=1e-6), (
        "real index must be ~110 (latest 110 over the frozen base 100), "
        "which only holds when base_price was not rewritten to 110"
    )


@pytest.mark.asyncio
async def test_frozen_baseline_ignores_same_month_ranking_shift(
    store: MarketHistoryStore,
) -> None:
    """A new item gaining the most buckets must not displace the frozen set."""
    names = await _seed_and_refresh(
        store, count=_TARGET, price=_P0, clock=_JAN_16_00, prefix="base"
    )
    before = _read_components(store)
    assert len(before) == _TARGET

    # A newcomer gets *more* buckets than any frozen item (no ties), so a
    # re-ranking would push it to the top of a 250-candidate list.
    bucket = _JAN_16_00 // 3600 * 3600
    later_bucket = bucket + 3600
    for back in range(8):
        _insert_snapshot(store, "newcomer", later_bucket - back * 3600, _P1)
    for index in range(6):  # frozen items keep 1 new bucket only
        _insert_snapshot(store, names[index], later_bucket, _P1)

    await store.refresh_equipment_components(now=_JAN_17_00)
    after = _read_components(store)
    assert after == before, "frozen component set must not be re-ranked"
    assert "newcomer" not in {row[0] for row in after}


@pytest.mark.asyncio
async def test_utc8_cross_month_rolls_baseline(store: MarketHistoryStore) -> None:
    """2026-09-30 23:30+08 builds; 2026-10-01 00:30+08 (same day in UTC)
    provides 250 new-month candidates -> whole baseline rolls to October,
    base day and prices update, index is ~100 again."""
    _assert_utc8(_SEP_30_2330, "2026-09-30 23:30")
    _assert_utc8(_OCT_01_0030, "2026-10-01 00:30")

    bucket_sep = _SEP30_23_BUCKET
    for index in range(_TARGET):
        _insert_snapshot(store, f"sep-{index:03d}", bucket_sep, _P0)
        _insert_snapshot(store, f"sep-{index:03d}", bucket_sep - 3600, _P0)
    await _refresh(store, clock=_SEP_30_2330, expected=_TARGET)
    before = _read_components(store)

    # October names priced in October buckets (same price, so the value is
    # exactly 100 after the roll).
    bucket_oct = _OCT1_00_BUCKET
    for index in range(_TARGET):
        _insert_snapshot(store, f"oct-{index:03d}", bucket_oct, _P0)
        _insert_snapshot(store, f"oct-{index:03d}", bucket_oct - 3600, _P0)
    await _refresh(store, clock=_OCT_01_0030, expected=_TARGET)

    after = _read_components(store)
    assert {row[0] for row in after} == {f"oct-{index:03d}" for index in range(_TARGET)}
    assert all(row[1] == "2026-10-01" for row in after)
    assert all(row[2] == pytest.approx(_P0) for row in after)
    # The September baseline is fully replaced, not appended to.
    assert {row[0] for row in before}.isdisjoint({row[0] for row in after})

    status = await EquipmentIndexService(store).status()
    assert status.ready is True
    assert status.base_day == "2026-10-01"
    assert status.latest_value == pytest.approx(100.0, abs=1e-6)


@pytest.mark.asyncio
async def test_new_month_thin_candidates_keep_old_baseline(
    store: MarketHistoryStore,
) -> None:
    """Old baseline 250; new month has only 100 candidates (old quotes are
    outside the new 30-day window and cannot leak in) -> old table rows
    unchanged, returns 250."""
    bucket = _SEP_05_1200 // 3600 * 3600
    for index in range(_TARGET):
        _insert_snapshot(store, f"old-{index:03d}", bucket, _P0)
        _insert_snapshot(store, f"old-{index:03d}", bucket - 3600, _P0)
    await _refresh(store, clock=_SEP_05_1200, expected=_TARGET)
    before = _read_components(store)

    # New-month refresh at 2026-10-15: its 30-day window starts 2026-09-15,
    # so the September-05 quotes sit outside and cannot add up.
    new_bucket = _OCT_15_1200 // 3600 * 3600
    for index in range(100):
        _insert_snapshot(store, f"new-{index:03d}", new_bucket, _P0)
        _insert_snapshot(store, f"new-{index:03d}", new_bucket - 3600, _P0)

    assert await store.refresh_equipment_components(now=_OCT_15_1200) == _TARGET
    assert _read_components(store) == before
    assert all(row[1] == "2026-09-05" for row in _read_components(store))


@pytest.mark.asyncio
async def test_new_month_no_candidates_keeps_old_baseline(
    store: MarketHistoryStore,
) -> None:
    """Old baseline 250; new month has no candidates at all -> old rows
    unchanged, returns 250, base day untouched."""
    bucket = _SEP_05_1200 // 3600 * 3600
    for index in range(_TARGET):
        _insert_snapshot(store, f"old-{index:03d}", bucket, _P0)
        _insert_snapshot(store, f"old-{index:03d}", bucket - 3600, _P0)
    await _refresh(store, clock=_SEP_05_1200, expected=_TARGET)
    before = _read_components(store)

    # Refresh in a later month with nothing collected yet.
    assert await store.refresh_equipment_components(now=_OCT_15_1200) == _TARGET
    assert _read_components(store) == before


@pytest.mark.asyncio
async def test_rebuild_failure_rolls_back_and_propagates(
    store: MarketHistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new-month rebuild that satisfies the gate must roll back when the
    real replace transaction fails after its first write; the old table
    stays fully intact and the exception propagates to the caller."""
    bucket = _SEP_05_1200 // 3600 * 3600
    for index in range(_TARGET):
        _insert_snapshot(store, f"old-{index:03d}", bucket, _P0)
        _insert_snapshot(store, f"old-{index:03d}", bucket - 3600, _P0)
    await _refresh(store, clock=_SEP_05_1200, expected=_TARGET)
    before = _read_components(store)

    # Seed a full new-month candidate pool.
    new_bucket = _OCT_15_1200 // 3600 * 3600
    for index in range(_TARGET):
        _insert_snapshot(store, f"new-{index:03d}", new_bucket, _P1)
        _insert_snapshot(store, f"new-{index:03d}", new_bucket - 3600, _P1)

    # Make only the refresh connection fail at COMMIT time — after the
    # replace started writing but before the old table is replaced.  Reads
    # and seeding use the plain sqlite3 driver and keep working; the
    # failure is injected on the test's own connection factory (patched
    # import), not by changing the production implementation.
    import src.services.market_history as market_history_module

    class Boom(RuntimeError):
        pass

    class FailingConnection(sqlite3.Connection):
        def commit(self) -> None:  # type: ignore[override]
            raise Boom("injected replace failure")

    def failing_connect(path: Path, *, timeout_seconds: float, label: str) -> sqlite3.Connection:
        connection = FailingConnection(path, timeout=timeout_seconds)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(market_history_module, "connect_local_database", failing_connect)
    with pytest.raises(Boom, match="injected replace failure"):
        await store.refresh_equipment_components(now=_OCT_15_1200)
    monkeypatch.undo()

    # The old baseline must be fully intact after reopening.
    assert _read_components(store) == before


@pytest.mark.asyncio
async def test_refresh_wraps_decision_in_explicit_transaction(
    store: MarketHistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh must run inside an explicit transaction: the first
    SELECT of the decision already observes ``in_transaction is True``
    (BEGIN IMMEDIATE ran before any read).  The check rides a real
    connection's trace callback — no mocked results, no production hook."""
    import src.services.market_history as market_history_module

    bucket = _JAN_16_00 // 3600 * 3600
    for index in range(10):
        _insert_snapshot(store, f"tx-{index:03d}", bucket, _P0)
        _insert_snapshot(store, f"tx-{index:03d}", bucket - 3600, _P0)

    observed: list[bool] = []
    original = market_history_module.connect_local_database

    def traced_connect(path: Path, *, timeout_seconds: float, label: str) -> sqlite3.Connection:
        connection = original(path, timeout_seconds=timeout_seconds, label=label)
        first = True

        def trace(sql: str) -> None:
            nonlocal first
            if first and sql.lstrip().upper().startswith("SELECT"):
                first = False
                observed.append(connection.in_transaction)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(market_history_module, "connect_local_database", traced_connect)
    result = await store.refresh_equipment_components(now=_JAN_16_00)
    monkeypatch.undo()

    assert result == 10
    assert observed, "no SELECT was traced — the refresh never read the database"
    assert observed[0] is True, (
        "first SELECT ran outside an explicit transaction: "
        "BEGIN IMMEDIATE must precede every read of the decision"
    )


@pytest.mark.asyncio
async def test_mixed_month_rows_force_rebuild_not_freeze(
    store: MarketHistoryStore,
) -> None:
    """A 250-row baseline whose LIMIT-1 row is in the current month while
    the remaining 249 rows are in the previous month must NOT be frozen:
    a row-wise freeze check would see only the first row's month and keep
    the stale table.  With fresh candidates available the refresh must
    rebuild to one consistent new-month baseline."""
    import sqlite3 as _sq

    # Build the mixed table directly.  The table is WITHOUT ROWID keyed by
    # market_name, so an ORDER-BY-less LIMIT 1 returns the alphabetically
    # first key — name the October row so it sorts first and is what the
    # current row-wise freeze check would see.
    connection = _sq.connect(store.path)
    try:
        connection.execute(
            "INSERT INTO equipment_index_components"
            " (market_name, base_day, base_price, added_at) VALUES (?, ?, ?, ?)",
            ("000-oct", "2026-10-15", _P0, _OCT_15_1200),
        )
        for index in range(_TARGET - 1):
            connection.execute(
                "INSERT INTO equipment_index_components"
                " (market_name, base_day, base_price, added_at) VALUES (?, ?, ?, ?)",
                (f"mix-{index:03d}", "2026-09-05", _P0, _SEP_05_1200),
            )
        connection.commit()
    finally:
        connection.close()

    # Precondition: the LIMIT-1 (arbitrary first) row belongs to October…
    probe = _sq.connect(store.path)
    try:
        first = probe.execute("SELECT base_day FROM equipment_index_components LIMIT 1").fetchone()
        months = {
            str(row[0])[:7]
            for row in probe.execute("SELECT base_day FROM equipment_index_components").fetchall()
        }
    finally:
        probe.close()
    assert first is not None and str(first[0]).startswith("2026-10"), (
        "precondition broken: the LIMIT-1 row is not in the current month"
    )
    assert months == {"2026-09", "2026-10"}, (
        f"precondition broken: expected mixed months, got {months}"
    )

    # Fresh October candidates covering the full set (the same 250 names
    # that sit in the mixed table, now quoted in October) -> refresh must
    # rebuild (replace), never freeze the mixed table.
    oct_bucket = _OCT_15_1200 // 3600 * 3600
    names = ["000-oct"] + [f"mix-{index:03d}" for index in range(_TARGET - 1)]
    for name in names:
        _insert_snapshot(store, name, oct_bucket, _P0)
        _insert_snapshot(store, name, oct_bucket - 3600, _P0)

    assert await store.refresh_equipment_components(now=_OCT_15_1200) == _TARGET
    after = _read_components(store)
    assert len(after) == _TARGET
    assert {row[0] for row in after} == set(names), "rebuild must use the fresh candidate set"
    assert {row[1] for row in after} == {"2026-10-15"}, (
        "refresh must rebuild to one consistent new-month baseline, "
        f"got mixed base days {sorted({row[1] for row in after})}"
    )
    status = await EquipmentIndexService(store).status()
    assert status.ready is True


@pytest.mark.asyncio
async def test_coverage_below_gate_still_refuses_publish(
    store: MarketHistoryStore,
) -> None:
    """A 250-item frozen baseline whose latest bucket covers only 200 items
    reports 80% coverage, ready=False and an empty index — the refresh must
    not shrink the component list to inflate the coverage ratio."""
    bucket = _JAN_16_00 // 3600 * 3600
    names = await _seed_and_refresh(
        store, count=_TARGET, price=_P0, clock=_JAN_16_00, prefix="base"
    )
    # Same month, next hour: only 200 of the 250 names have a fresh quote.
    later_bucket = bucket + 3600
    for name in names[:_MIN]:
        _insert_snapshot(store, name, later_bucket, _P0)

    result = await store.refresh_equipment_components(now=_JAN_17_00)
    assert result == _TARGET  # the frozen baseline is not shrunk

    status = await EquipmentIndexService(store).status()
    assert status.components == _TARGET
    assert status.covered == _MIN
    assert status.coverage == pytest.approx(_MIN / _TARGET)
    assert status.ready is False
    assert status.latest_value is None
