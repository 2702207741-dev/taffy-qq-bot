"""Synthetic-SQLite tests for the equipment production rule unification and
the internal rebase transaction functions.

Every scenario drives the REAL code paths (MarketHistoryStore.refresh /
equipment_rebase_txn) against a fresh temporary SQLite database shaped by
the production schema.  Nothing is mocked into success; the transaction
functions are only ever exercised here (no apply entry point exists).

Every SQL statement in this file is an inline literal; all values travel
through ``?`` parameter binding.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from src.services import equipment_rebase_txn as txn
from src.services import market_history as mh

BASE = 1_000_000 * 3600  # an arbitrary hour-aligned bucket
BASE_DAY = mh._market_day(BASE)


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(mh._SCHEMA)
    return conn


def _seed_quotes(conn: sqlite3.Connection, rows: list[tuple[int, str, float]]) -> None:
    # The production schema requires data_time; seeds use the bucket itself.
    conn.executemany(
        "INSERT INTO quote_snapshots (bucket_at, market_name, sell_price, data_time) "
        "VALUES (?, ?, ?, ?)",
        [
            (bucket_at, market_name, sell_price, bucket_at)
            for bucket_at, market_name, sell_price in rows
        ],
    )
    conn.commit()


def _seed_components(conn: sqlite3.Connection, rows: list[tuple[str, str, float, int]]) -> None:
    conn.executemany(
        "INSERT INTO equipment_index_components "
        "(market_name, base_day, base_price, added_at) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _component_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT market_name FROM equipment_index_components ORDER BY market_name COLLATE BINARY"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _table_dump(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Deterministic dump of the non-component tables this module must never
    write.  Every SQL statement is an inline literal."""
    dump: dict[str, list[str]] = {}
    quote_rows = conn.execute("SELECT * FROM quote_snapshots").fetchall()
    dump["quote_snapshots"] = sorted(repr(r) for r in quote_rows)
    state_rows = conn.execute("SELECT * FROM collection_state").fetchall()
    dump["collection_state"] = sorted(repr(r) for r in state_rows)
    run_rows = conn.execute("SELECT * FROM collection_runs").fetchall()
    dump["collection_runs"] = sorted(repr(r) for r in run_rows)
    return dump


def _valid_quotes(conn: sqlite3.Connection, count: int = 250) -> None:
    """`count` names each with two buckets (base-3600 and base), priced."""
    rows: list[tuple[int, str, float]] = []
    for i in range(count):
        rows.append((BASE - 3600, f"top{i:03d}", 10.0))
        rows.append((BASE, f"top{i:03d}", 10.0))
    _seed_quotes(conn, rows)


# --- production refresh: frozen baseline is never rewritten by the update ---


@pytest.mark.asyncio
async def test_frozen_baseline_not_rewritten_by_rule_update(tmp_path: Path) -> None:
    """A same-month frozen baseline (>=200, all current UTC+8 month) is left
    byte-identical even though quote data would rank differently: the
    refresh must return early and never rewrite it."""
    path = tmp_path / "frozen.db"
    store = mh.MarketHistoryStore(path=path)
    await store.initialize()
    conn = sqlite3.connect(path)
    _seed_components(conn, [(f"frozen-{i:03d}", BASE_DAY, 5.0, 1) for i in range(250)])
    _valid_quotes(conn, 260)
    before = txn.fingerprint_components(conn)
    conn.close()
    count = await store.refresh_equipment_components(now=BASE)
    conn = sqlite3.connect(path)
    assert count == 250
    assert _component_names(conn) == [f"frozen-{i:03d}" for i in range(250)]
    assert txn.fingerprint_components(conn) == before, "frozen baseline untouched"
    conn.close()


# --- production refresh: shared three-key ordering + fallback age gate ---


@pytest.mark.asyncio
async def test_refresh_uses_shared_three_key_ordering(tmp_path: Path) -> None:
    """Empty-table refresh must select by the SHARED three-key order: with
    249 two-bucket names + a tied pair at ranks 250..251, the name key
    admits tie-a and drops tie-b (a two-key order could not decide)."""
    path = tmp_path / "order.db"
    store = mh.MarketHistoryStore(path=path)
    await store.initialize()
    conn = sqlite3.connect(path)
    rows: list[tuple[int, str, float]] = []
    for i in range(249):
        rows.append((BASE - 3600, f"top{i:03d}", 10.0))
        rows.append((BASE, f"top{i:03d}", 10.0))
    rows.append((BASE - 3600, "tie-a", 5.0))
    rows.append((BASE - 3600, "tie-b", 6.0))
    _seed_quotes(conn, rows)
    conn.close()
    await store.refresh_equipment_components(now=BASE)
    conn = sqlite3.connect(path)
    names = _component_names(conn)
    conn.close()
    assert len(names) == 250
    assert "tie-a" in names and "tie-b" not in names
    assert all(n in names for n in [f"top{i:03d}" for i in range(249)])


@pytest.mark.asyncio
async def test_refresh_refuses_whole_when_selected_member_aged(tmp_path: Path) -> None:
    """A member whose only quotes are 25h+ old OCCUPIES a top-250 slot
    (300 buckets outrank every two-bucket name).  The real production
    refresh must refuse the WHOLE refresh: the old component table (built
    as a NON-frozen cross-month baseline so the early-return path cannot
    fake a pass) stays complete and untouched — the remaining 249 valid
    members are never written and top249 is never pulled in as a refill."""
    path = tmp_path / "aged.db"
    store = mh.MarketHistoryStore(path=path)
    await store.initialize()
    conn = sqlite3.connect(path)
    # Old baseline in a PREVIOUS month (not frozen for the current month).
    old_day = mh._market_day(BASE - 35 * 86400)
    old_rows = [(f"old-{i}", old_day, 9.0, 1) for i in range(2)]
    _seed_components(conn, old_rows)
    before = txn.fingerprint_components(conn)
    rows: list[tuple[int, str, float]] = []
    for i in range(250):
        rows.append((BASE - 3600, f"top{i:03d}", 10.0))
        rows.append((BASE, f"top{i:03d}", 10.0))
    for k in range(300):
        rows.append((BASE - 90000 - k * 3600, "aged-star", 5.0))
    _seed_quotes(conn, rows)
    conn.close()
    count = await store.refresh_equipment_components(now=BASE)
    conn = sqlite3.connect(path)
    assert count == 2, "the refresh must refuse and keep the old table's count"
    assert txn.fingerprint_components(conn) == before, "old table must stay byte-identical"
    assert _component_names(conn) == [f"old-{i}" for i in range(2)]
    conn.close()


# --- internal transaction functions ---


def test_apply_refuses_whole_when_selected_member_aged(tmp_path: Path) -> None:
    """One selected member with an aged fallback refuses the WHOLE rebase;
    the old table stays intact (transaction-level gate, no refill)."""
    path = tmp_path / "t1.db"
    conn = _conn(path)
    rows: list[tuple[int, str, float]] = []
    for i in range(249):
        rows.append((BASE - 3600, f"top{i:03d}", 10.0))
        rows.append((BASE, f"top{i:03d}", 10.0))
    rows.append((BASE - 90000, "aged-x", 5.0))  # rank 250, fallback 25h old
    _seed_quotes(conn, rows)
    _seed_components(conn, [("legacy", BASE_DAY, 9.0, 1)])
    before = txn.fingerprint_components(conn)
    with pytest.raises(txn.EquipmentRebaseError, match="refused"):
        txn.apply_equipment_rebase(conn, now=BASE + 60)
    assert txn.fingerprint_components(conn) == before, "old table must be intact"
    assert _component_names(conn) == ["legacy"]
    conn.close()


def test_apply_refuses_on_fingerprint_conflict(tmp_path: Path) -> None:
    path = tmp_path / "t2.db"
    conn = _conn(path)
    _valid_quotes(conn)
    _seed_components(conn, [("legacy", BASE_DAY, 9.0, 1)])
    real = txn.fingerprint_components(conn)
    with pytest.raises(txn.FingerprintMismatch):
        txn.apply_equipment_rebase(conn, now=BASE + 60, expected_fingerprint="0" * 64)
    assert txn.fingerprint_components(conn) == real
    conn.close()


def test_apply_succeeds_and_targeted_rollback_restores(tmp_path: Path) -> None:
    path = tmp_path / "t3.db"
    conn = _conn(path)
    _valid_quotes(conn)
    old_rows = [
        ("legacy-a", BASE_DAY, 9.0, 1),
        ("legacy-b", BASE_DAY, 8.0, 1),
        ("legacy-c", BASE_DAY, 7.0, 1),
    ]
    _seed_components(conn, old_rows)
    fp_old = txn.fingerprint_components(conn)
    outcome = txn.apply_equipment_rebase(conn, now=BASE + 60, expected_fingerprint=fp_old)
    assert outcome.components == 250
    assert outcome.covered == 250
    assert outcome.fallback == 0
    assert outcome.coverage == 1.0
    assert outcome.applied_at == BASE + 60
    assert outcome.bucket_b == BASE
    assert _component_names(conn) == [f"top{i:03d}" for i in range(250)]
    fp_applied = txn.fingerprint_components(conn)
    assert fp_applied == outcome.fingerprint_after
    # Targeted rollback with the APPLIED fingerprint restores the old table.
    restored = txn.rollback_equipment_components(
        conn, expected_current_fingerprint=fp_applied, archived_rows=old_rows
    )
    assert restored == 3
    assert txn.fingerprint_components(conn) == fp_old
    assert _component_names(conn) == ["legacy-a", "legacy-b", "legacy-c"]
    conn.close()


def test_rollback_refused_after_later_legitimate_change(tmp_path: Path) -> None:
    path = tmp_path / "t4.db"
    conn = _conn(path)
    _valid_quotes(conn)
    old_rows = [("legacy", BASE_DAY, 9.0, 1)]
    _seed_components(conn, old_rows)
    fp_old = txn.fingerprint_components(conn)
    outcome = txn.apply_equipment_rebase(conn, now=BASE + 60, expected_fingerprint=fp_old)
    # A later legitimate change (e.g. next month's fresh baseline update).
    conn.execute(
        "UPDATE equipment_index_components SET base_day = ? WHERE market_name = ?",
        ("2999-01-01", "top000"),
    )
    conn.commit()
    with pytest.raises(txn.FingerprintMismatch, match="refusing targeted rollback"):
        txn.rollback_equipment_components(
            conn,
            expected_current_fingerprint=outcome.fingerprint_after,
            archived_rows=old_rows,
        )
    # The later change survives; nothing was overwritten.
    day = conn.execute(
        "SELECT base_day FROM equipment_index_components WHERE market_name = ?",
        ("top000",),
    ).fetchone()
    assert day == ("2999-01-01",)
    conn.close()


def test_freshness_boundary_exact_7200_passes(tmp_path: Path) -> None:
    path = tmp_path / "t5.db"
    conn = _conn(path)
    _valid_quotes(conn)
    fp_old = txn.fingerprint_components(conn)
    outcome = txn.apply_equipment_rebase(conn, now=BASE + 7200, expected_fingerprint=fp_old)
    assert outcome.applied_at - outcome.bucket_b == 7200
    assert outcome.components == 250
    conn.close()


def test_freshness_over_7200_refused(tmp_path: Path) -> None:
    path = tmp_path / "t6.db"
    conn = _conn(path)
    _valid_quotes(conn)
    before = txn.fingerprint_components(conn)
    with pytest.raises(txn.FreshnessViolation, match="T-B"):
        txn.apply_equipment_rebase(conn, now=BASE + 7201)
    assert txn.fingerprint_components(conn) == before
    conn.close()


def test_future_bucket_refused(tmp_path: Path) -> None:
    path = tmp_path / "t7.db"
    conn = _conn(path)
    _valid_quotes(conn)
    with pytest.raises(txn.FreshnessViolation, match="future B"):
        txn.apply_equipment_rebase(conn, now=BASE - 1)
    conn.close()


def test_no_quotes_refused(tmp_path: Path) -> None:
    path = tmp_path / "t8.db"
    conn = _conn(path)
    with pytest.raises(txn.EquipmentRebaseError, match="no quote buckets"):
        txn.apply_equipment_rebase(conn, now=BASE)
    conn.close()


def test_non_aligned_latest_bucket_refused_is_guaranteed_by_schema(
    tmp_path: Path,
) -> None:
    """The production schema itself rejects non-hour-aligned buckets
    (CHECK bucket_at % 3600 = 0), so the transaction layer's defensive
    hour-alignment check cannot be reached with real data — it is kept as a
    guard and verified indirectly: only aligned data can be seeded."""
    path = tmp_path / "t9.db"
    conn = _conn(path)
    _valid_quotes(conn, 1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO quote_snapshots (bucket_at, market_name, sell_price, "
            "data_time) VALUES (?, ?, ?, ?)",
            (BASE + 1, "misaligned", 2.0, BASE + 1),
        )
    conn.close()


def test_midway_failure_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure AFTER the DELETE+INSERT steps must roll the whole
    transaction back: the old component table stays byte-identical and no
    half-written state survives.  The failure is injected into the SECOND
    fingerprint read (computed after the component INSERT) — the only
    honest injection point, since the schema's CHECKs already reject
    over-long names, misaligned buckets and non-positive prices."""
    path = tmp_path / "t10.db"
    conn = _conn(path)
    _seed_components(conn, [("legacy", BASE_DAY, 9.0, 1)])
    _valid_quotes(conn)
    before = txn.fingerprint_components(conn)
    real_fingerprint = txn.fingerprint_components
    calls = {"count": 0}

    def flaky_fingerprint(connection) -> str:
        calls["count"] += 1
        if calls["count"] == 2:  # the post-INSERT fingerprint read
            raise RuntimeError("synthetic mid-transaction failure")
        return real_fingerprint(connection)

    monkeypatch.setattr(txn, "fingerprint_components", flaky_fingerprint)
    with pytest.raises(RuntimeError, match="synthetic mid-transaction failure"):
        txn.apply_equipment_rebase(conn, now=BASE + 60)
    assert calls["count"] == 2, "the post-INSERT fingerprint read must have run"
    assert txn.fingerprint_components(conn) == before, "rollback must restore the old table"
    assert _component_names(conn) == ["legacy"]
    # The connection is usable afterwards (no dangling transaction).
    assert conn.execute("SELECT COUNT(*) FROM equipment_index_components").fetchone() == (1,)
    conn.close()


def test_unrelated_tables_untouched_by_apply_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "t11.db"
    conn = _conn(path)
    _valid_quotes(conn)
    _seed_components(conn, [("legacy", BASE_DAY, 9.0, 1)])
    conn.execute(
        "INSERT INTO collection_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("k", "v", 1),
    )
    conn.execute(
        "INSERT INTO collection_runs (bucket_at, catalog_count, requested_count, "
        "valid_count, updated_at) VALUES (?, ?, ?, ?, ?)",
        (BASE, 1, 1, 1, 1),
    )
    conn.commit()
    before = _table_dump(conn)
    fp_old = txn.fingerprint_components(conn)
    outcome = txn.apply_equipment_rebase(conn, now=BASE + 60, expected_fingerprint=fp_old)
    assert _table_dump(conn) == before, "apply must not touch non-component tables"
    txn.rollback_equipment_components(
        conn,
        expected_current_fingerprint=outcome.fingerprint_after,
        archived_rows=[("legacy", BASE_DAY, 9.0, 1)],
    )
    assert _table_dump(conn) == before, "rollback must not touch non-component tables"
    conn.close()


# --- approved-proposal entry (real-clock, proposal-bound) ---


def _now_hour() -> int:
    """The CURRENT wall-clock hour bucket, so the no-``now`` approved entry
    always satisfies 0 <= T-B <= 3600s regardless of when tests run."""
    return int(time.time()) // 3600 * 3600


def _approved_like_db(path: Path) -> tuple[sqlite3.Connection, int]:
    """DB whose latest bucket is the current wall-clock hour: 250 names
    with 25 hourly buckets each (ending at B, priced) plus a small legacy
    component table that is NOT frozen for the current month."""
    conn = _conn(path)
    b = _now_hour()
    rows: list[tuple[int, str, float]] = []
    for i in range(250):
        name = f"top{i:03d}"
        price = 10.0 + (i % 5)
        for k in range(25):
            rows.append((b - (k + 1) * 3600, name, price))
        rows.append((b, name, price))
    _seed_quotes(conn, rows)
    _seed_components(
        conn,
        [
            ("legacy-a", "2026-08-01", 9.0, 1),
            ("legacy-b", "2026-08-01", 8.0, 1),
        ],
    )
    return conn, b


def _current_proposal(
    conn: sqlite3.Connection, b: int, *, reference: int | None = None
) -> dict[str, Any]:
    """A proposal equal to what the shared rules compute on this database
    RIGHT NOW — the reviewed-and-approved baseline for the tests.  ``b`` is
    the cutoff; ``reference`` defaults to ``b`` but may be any reviewed
    instant (the ranking window is ``reference - 30d .. b``)."""
    r = b if reference is None else reference
    since = r - 30 * 86400
    entries, covered, fallback, total, rejected = txn._select_and_price(
        conn, since=since, bucket_b=b
    )
    assert not rejected
    assert len(entries) >= 200
    components: list[dict[str, Any]] = [
        {"market_name": name, "base_price": price} for name, price in sorted(entries)
    ]
    return {"cutoff": b, "reference": r, "components": components}


def _component_dump(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT market_name, base_day, base_price, added_at "
        "FROM equipment_index_components ORDER BY market_name COLLATE BINARY"
    ).fetchall()
    return [repr(r) for r in rows]


def test_approved_execution_refuses_when_quotes_changed_after_review(
    tmp_path: Path,
) -> None:
    """A quote that changed AFTER the proposal was reviewed (while the old
    component table stayed identical) must refuse the execution."""
    path = tmp_path / "ap1.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    before = _component_dump(conn)
    # Change one member's cutoff price after "review".
    conn.execute(
        "UPDATE quote_snapshots SET sell_price = ? WHERE market_name = ? AND bucket_at = ?",
        (999.0, "top005", b),
    )
    conn.commit()
    # Unrelated tables as they are RIGHT BEFORE the refused execution (the
    # quote edit above is the test's injected change, not an execution side
    # effect).
    unrelated_before = _table_dump(conn)
    with pytest.raises(txn.ProposalMismatch, match="base prices differ"):
        txn.apply_approved_rebase(
            conn,
            proposal=proposal,
            approved_fingerprint=approved,
            expected_old_fingerprint=fp_old,
        )
    assert _component_dump(conn) == before, "old component table must be intact"
    assert _table_dump(conn) == unrelated_before, "unrelated tables must be intact"
    conn.close()


def test_approved_execution_refuses_when_latest_bucket_advanced(tmp_path: Path) -> None:
    """A newer quote bucket must never be adopted silently: the live
    MAX(bucket_at) differing from the approved cutoff refuses the run."""
    path = tmp_path / "ap2.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    before = _component_dump(conn)
    # The market advances one hour after the review.
    _seed_quotes(conn, [(b + 3600, "top000", 10.0)])
    with pytest.raises(txn.ProposalMismatch, match="refusing to silently adopt"):
        txn.apply_approved_rebase(
            conn,
            proposal=proposal,
            approved_fingerprint=approved,
            expected_old_fingerprint=fp_old,
        )
    assert _component_dump(conn) == before
    conn.close()


def test_approved_execution_refuses_on_fingerprint_or_content_mismatch(
    tmp_path: Path,
) -> None:
    """Neither a wrong approved fingerprint nor a silently edited proposal
    payload may execute."""
    path = tmp_path / "ap3.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    before = _component_dump(conn)
    with pytest.raises(txn.ProposalMismatch, match="approved fingerprint"):
        txn.apply_approved_rebase(
            conn,
            proposal=proposal,
            approved_fingerprint="0" * 64,
            expected_old_fingerprint=fp_old,
        )
    assert _component_dump(conn) == before
    edited = {
        "cutoff": proposal["cutoff"],
        "reference": proposal["reference"],
        "components": [dict(m) for m in proposal["components"]],
    }
    edited["components"][0]["base_price"] = 12345.0
    with pytest.raises(txn.ProposalMismatch, match="approved fingerprint"):
        txn.apply_approved_rebase(
            conn,
            proposal=edited,
            approved_fingerprint=approved,
            expected_old_fingerprint=fp_old,
        )
    assert _component_dump(conn) == before
    conn.close()


def test_approved_execution_succeeds_when_proposal_matches(tmp_path: Path) -> None:
    """A fully matching approved proposal — after a JSON round trip, which
    is how a reviewed dry-run proposal reaches the execution step — executes
    atomically and returns the actual time, adopted parameters, both
    fingerprints and the archived old rows needed for a targeted rollback.
    A JSON round trip of finite doubles is exact, so the exact price
    comparison must still pass."""
    import json

    path = tmp_path / "ap4.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    # JSON round trip (the reviewed proposal travelled as JSON).
    proposal = json.loads(json.dumps(proposal, ensure_ascii=False))
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    unrelated_before = _table_dump(conn)
    result = txn.apply_approved_rebase(
        conn,
        proposal=proposal,
        approved_fingerprint=approved,
        expected_old_fingerprint=fp_old,
    )
    assert result.bucket_b == b
    assert result.reference == b
    assert result.components == 250
    assert result.covered == 250
    assert result.fallback == 0
    assert result.fingerprint_before == fp_old
    assert result.fingerprint_after != fp_old
    assert 0 <= result.applied_at - result.bucket_b <= 7200
    assert len(result.archived_rows) == 2
    assert {r[0] for r in result.archived_rows} == {"legacy-a", "legacy-b"}
    assert _table_dump(conn) == unrelated_before, "unrelated tables must be intact"
    # Targeted rollback with the returned material restores the old table.
    txn.rollback_equipment_components(
        conn,
        expected_current_fingerprint=result.fingerprint_after,
        archived_rows=result.archived_rows,
    )
    assert txn.fingerprint_components(conn) == fp_old
    conn.close()


def test_approved_execution_accepts_reference_later_than_cutoff(
    tmp_path: Path,
) -> None:
    """R and B are separate reviewed parameters: a legal proposal with
    R = B + 1s (a non-hour-aligned, non-equal reference) must execute.  The
    window is computed from the approved R, not forced to B."""
    path = tmp_path / "ap5.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b, reference=b + 1)
    assert proposal["reference"] == b + 1
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    result = txn.apply_approved_rebase(
        conn,
        proposal=proposal,
        approved_fingerprint=approved,
        expected_old_fingerprint=fp_old,
    )
    assert result.bucket_b == b
    assert result.reference == b + 1
    assert result.components == 250
    conn.close()


def test_approved_execution_requires_old_fingerprint(tmp_path: Path) -> None:
    """The reviewed old-baseline fingerprint is mandatory: omitting the
    argument (no default) or passing an empty value must refuse — the check
    is never skipped."""
    path = tmp_path / "ap6.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    before = _component_dump(conn)
    with pytest.raises(TypeError):
        txn.apply_approved_rebase(  # type: ignore[call-arg]  # deliberate: missing required keyword
            conn, proposal=proposal, approved_fingerprint=approved
        )
    assert _component_dump(conn) == before
    with pytest.raises(txn.ProposalMismatch, match="required, never skipped"):
        txn.apply_approved_rebase(
            conn,
            proposal=proposal,
            approved_fingerprint=approved,
            expected_old_fingerprint="",
        )
    assert _component_dump(conn) == before
    conn.close()


def test_approved_execution_refuses_when_old_table_changed(tmp_path: Path) -> None:
    """The old component table changing between review and execution (even
    when quotes are untouched) refuses via the fingerprint re-read."""
    path = tmp_path / "ap7.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    conn.execute(
        "UPDATE equipment_index_components SET base_price = ? WHERE market_name = ?",
        (99.0, "legacy-a"),
    )
    conn.commit()
    before = _component_dump(conn)
    with pytest.raises(txn.FingerprintMismatch, match="differs from the reviewed one"):
        txn.apply_approved_rebase(
            conn,
            proposal=proposal,
            approved_fingerprint=approved,
            expected_old_fingerprint=fp_old,
        )
    assert _component_dump(conn) == before
    conn.close()


def test_approved_execution_refuses_price_delta_below_old_tolerance(
    tmp_path: Path,
) -> None:
    """Prices are compared EXACTLY: a delta far below the removed 1e-9
    tolerance (1e-12), which SQLite REAL can store, must still refuse the
    execution."""
    path = tmp_path / "ap8.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    before = _component_dump(conn)
    # top005's proposal price is exactly 10.0; nudge the live quote by an
    # amount smaller than the former 1e-9 tolerance.
    tiny = 10.0 + 1e-12
    conn.execute(
        "UPDATE quote_snapshots SET sell_price = ? WHERE market_name = ? AND bucket_at = ?",
        (tiny, "top005", b),
    )
    conn.commit()
    stored = conn.execute(
        "SELECT sell_price FROM quote_snapshots WHERE market_name = ? AND bucket_at = ?",
        ("top005", b),
    ).fetchone()
    assert stored == (tiny,), "SQLite REAL must store the tiny delta exactly"
    with pytest.raises(txn.ProposalMismatch, match="base prices differ"):
        txn.apply_approved_rebase(
            conn,
            proposal=proposal,
            approved_fingerprint=approved,
            expected_old_fingerprint=fp_old,
        )
    assert _component_dump(conn) == before
    conn.close()


def test_approved_execution_holds_write_lock_for_checks_and_replace(
    tmp_path: Path,
) -> None:
    """Lock-conflict evidence, stated precisely: ``apply_approved_rebase``
    issues BEGIN IMMEDIATE as its first statement and keeps every read,
    validation, replace and commit inside that one reserved-write
    transaction, so with another connection already holding the write lock
    the entry fails with a lock error instead of running lock-free checks.

    This test proves the lock conflict is refused — it does NOT by itself
    prove that no other race window exists anywhere; the guarantee scope is
    the single-database writer serialisation described in the function
    docstring (no interleaving writer between checks and replace on THIS
    database)."""
    path = tmp_path / "ap9.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        blocked = sqlite3.connect(path, timeout=0.2)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            txn.apply_approved_rebase(
                blocked,
                proposal=proposal,
                approved_fingerprint=approved,
                expected_old_fingerprint=fp_old,
            )
        blocked.close()
    finally:
        holder.rollback()
        holder.close()
    # The failed attempt wrote nothing.
    assert _component_dump(conn) == _component_dump(sqlite3.connect(path))
    conn.close()


def test_approved_execution_midway_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after DELETE+INSERT (injected into the second fingerprint
    read) rolls the whole approved execution back: old component table and
    unrelated tables stay byte-identical."""
    path = tmp_path / "ap6.db"
    conn, b = _approved_like_db(path)
    proposal = _current_proposal(conn, b)
    approved = txn.proposal_fingerprint(proposal)
    fp_old = txn.fingerprint_components(conn)
    before = _component_dump(conn)
    unrelated_before = _table_dump(conn)
    real_fingerprint = txn.fingerprint_components
    calls = {"count": 0}

    def flaky_fingerprint(connection) -> str:
        calls["count"] += 1
        if calls["count"] == 2:  # the post-INSERT fingerprint read
            raise RuntimeError("synthetic mid-transaction failure")
        return real_fingerprint(connection)

    monkeypatch.setattr(txn, "fingerprint_components", flaky_fingerprint)
    with pytest.raises(RuntimeError, match="synthetic mid-transaction failure"):
        txn.apply_approved_rebase(
            conn,
            proposal=proposal,
            approved_fingerprint=approved,
            expected_old_fingerprint=fp_old,
        )
    assert calls["count"] == 2
    assert _component_dump(conn) == before, "old component table must be intact"
    assert _table_dump(conn) == unrelated_before, "unrelated tables must be intact"
    conn.close()
