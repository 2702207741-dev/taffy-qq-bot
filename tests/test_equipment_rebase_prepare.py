"""Tests for the equipment-rebase dry-run tool (synthetic temp databases).

Every scenario uses a fresh temporary SQLite database shaped like the
production snapshot schema (quote_snapshots / equipment_index_components)
and drives the real ``prepare_equipment_rebase`` code paths — nothing is
mocked into success.  The source database main file is byte-hashed before
and after to prove the tool never writes to it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import prepare_equipment_rebase as tool

SCHEMA = """
CREATE TABLE quote_snapshots (
    bucket_at INTEGER NOT NULL,
    market_name TEXT NOT NULL,
    sell_price REAL,
    PRIMARY KEY (bucket_at, market_name)
);
CREATE TABLE equipment_index_components (
    market_name TEXT PRIMARY KEY,
    base_day TEXT NOT NULL,
    base_price REAL NOT NULL,
    added_at INTEGER NOT NULL
);
"""


def _make_db(path: Path, hour: int = 1_000 * 3600) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _fresh_db(tmp_path: Path, name: str) -> tuple[Path, sqlite3.Connection]:
    """Each call gets its own file so re-running a test cannot collide."""
    path = tmp_path / name
    if path.exists():
        path.unlink()
    return path, _make_db(path)


def _seed(conn: sqlite3.Connection, rows: list[tuple[int, str, float]]) -> None:
    conn.executemany(
        "INSERT INTO quote_snapshots (bucket_at, market_name, sell_price) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


def _seed_old(conn: sqlite3.Connection, components: list[tuple[str, str, float, int]]) -> None:
    conn.executemany(
        "INSERT INTO equipment_index_components "
        "(market_name, base_day, base_price, added_at) VALUES (?, ?, ?, ?)",
        components,
    )
    conn.commit()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def _candidates_named(result: tool.Result) -> dict[str, tool.Candidate]:
    return {c.market_name: c for c in result.candidates}


def test_cutoff_bucket_hit_and_item_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Latest-bucket hit for covered items, item fallback for the rest."""
    base = 100_000 * 3600  # an arbitrary aligned hour
    conn = _make_db(tmp_path / "a.db")
    rows: list[tuple[int, str, float]] = []
    for i in range(5):
        rows.append((base - 3600, f"hit-{i}", 10.0 + i))  # present at cutoff too
        rows.append((base, f"hit-{i}", 11.0 + i))
    rows.append((base - 2 * 3600, "stale", 99.0))  # no cutoff row -> fallback
    _seed(conn, rows)
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)

    result = tool.compute(conn, cutoff=base, reference=base)
    by_name = _candidates_named(result)
    assert by_name["hit-0"].price_source == "cutoff-bucket-hit"
    assert by_name["hit-0"].base_price == 11.0
    assert by_name["stale"].price_source == "item-fallback"
    assert by_name["stale"].base_price == 99.0
    assert by_name["stale"].source_bucket == base - 2 * 3600
    assert by_name["stale"].quote_age_buckets == 2
    conn.close()


def test_data_after_cutoff_is_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "b.db")
    _seed(
        conn,
        [
            (base - 3600, "a", 5.0),
            (base, "a", 6.0),
            (base + 3600, "a", 7.0),  # after cutoff: must never be the price
            (base + 3600, "later-only", 8.0),  # only exists after cutoff
        ],
    )
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base)
    by_name = _candidates_named(result)
    assert by_name["a"].base_price == 6.0
    assert "later-only" not in by_name
    conn.close()


def test_gates_fail_honestly_when_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "c.db")
    _seed(conn, [(base, f"m{i}", 1.0) for i in range(5)])
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base)
    assert len(result.candidates) < tool.MIN_COMPONENTS
    conn.close()


def test_non_positive_price_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "d.db")
    _seed(conn, [(base, "bad", 0.0), (base, "ok", 1.0)])
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base)
    assert any(r["market_name"] == "bad" for r in result.rejected)
    assert "ok" in _candidates_named(result)
    conn.close()


def test_fixed_input_index_is_about_100(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "e.db")
    rows = []
    for i in range(tool.MIN_COMPONENTS + 10):
        rows.append((base - 3600, f"m{i}", 100.0 + i))
        rows.append((base, f"m{i}", 100.0 + i))
    _seed(conn, rows)
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base)
    assert len(result.candidates) >= tool.MIN_COMPONENTS
    price_map = {c.market_name: c.base_price for c in result.candidates}
    value = tool.index_value(result.candidates, price_map)
    assert value is not None and abs(value - 100.0) < 1e-9
    conn.close()


def test_old_components_preserved_and_source_bytes_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = 100_000 * 3600
    source = tmp_path / "source.db"
    conn = _make_db(source, hour=base)
    _seed(conn, [(base, "a", 5.0), (base, "b", 6.0)])
    _seed_old(conn, [("a", "2026-09-01", 5.0, 1), ("legacy", "2026-09-01", 9.0, 1)])
    before = _sha(source)
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    old = tool.read_old_components(conn)
    assert old["legacy"]["base_price"] == 9.0
    assert _sha(source) == before, "source main file bytes changed"
    conn.close()


def test_paths_reject_production_and_output_not_overwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The approved snapshot is the backup copy, never the production file;
    an existing output dir is refused by main()."""
    # Guard 1: the tool's default SNAPSHOT must be the approved backup path,
    # not the production database.
    snapshot_text = str(tool.SNAPSHOT).replace("\\", "/")
    assert snapshot_text.endswith("data/backups/equipment-rebase/market_history.db"), snapshot_text
    assert tool.SNAPSHOT != tool._PROJECT_ROOT / "data/market_history.db"

    # Guard 2: main() refuses to write into an existing output directory.
    out = tmp_path / "existing-out"
    out.mkdir()
    fake_snapshot = tmp_path / "fake-snapshot.db"
    sqlite3.connect(fake_snapshot).close()  # an existing file is enough
    monkeypatch.setattr(tool, "RUN_ID", "fixed")
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(tool, "SNAPSHOT", fake_snapshot)
    monkeypatch.setattr(tool, "open_ro", lambda path: sqlite3.connect(":memory:"))
    assert tool.main(["--out-dir", str(out)]) == 2
    assert not (out / "report.md").exists(), "existing output must not be overwritten"


def test_missing_evidence_reports_honestly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No rows at all -> empty candidate list; tool output must say so."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    base = 100_000 * 3600
    result = tool.compute(conn, cutoff=base, reference=base)
    assert result.candidates == []
    assert result.rejected == []
    conn.close()


def test_ranking_ties_still_reported_for_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A (buckets, last_bucket) pair with several members is reported for
    audit even though the rehearsal ordering (third key market_name COLLATE
    BINARY ASC) makes membership deterministic.  Both members sit inside the
    top N here, so it is an INTERNAL tie, not a boundary one."""
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "f.db")
    _seed(
        conn,
        [
            (base - 3600, "a", 1.0),
            (base, "a", 2.0),
            (base - 3600, "b", 3.0),
            (base, "b", 4.0),
        ],
    )
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base, old=None)
    # a and b share (buckets=2, last_bucket=base): the pair must be surfaced
    # as an internal tie for audit; no boundary resolution is needed.
    assert any(entry["pair"] == [2, base] for entry in result.ranking_ties_internal)
    assert result.boundary_resolutions == []
    conn.close()


def test_non_100_index_with_real_cutoff_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index must use the real cutoff-bucket price, so a uniform 10%
    drift between an earlier bucket and the cutoff yields a genuine non-100
    reading (counter-example to the old fixed-input==base shortcut)."""
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "g.db")
    rows = []
    for i in range(tool.MIN_COMPONENTS + 10):
        rows.append((base - 3600, f"m{i}", 100.0))  # earlier bucket
        rows.append((base, f"m{i}", 110.0))  # cutoff: 10% higher
    _seed(conn, rows)
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base, old=None)
    assert len(result.candidates) >= tool.MIN_COMPONENTS
    # Real cutoff prices (110) vs proposed base (110) -> ~100 by design.
    real = {c.market_name: c.base_price for c in result.candidates}
    value = tool.index_value(result.candidates, real)
    assert value is not None and abs(value - 100.0) < 1e-9
    # Counter-example: index earlier-bucket prices (100) against the
    # proposed base (110) -> ~90.9, NOT 100.
    earlier = {c.market_name: 100.0 for c in result.candidates}
    drifted = tool.index_value(result.candidates, earlier)
    assert drifted is not None
    assert abs(drifted - 100.0) > 1.0, "non-100 index drift must be observable"
    assert abs(drifted - (100.0 / 1.10)) < 0.01
    conn.close()


def test_invalid_price_does_not_shrink_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage is measured against the full candidate set: replacing a
    price with an invalid value must NOT make the ratio pass by shrinking
    the denominator."""
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "h.db")
    # 300 names all priced at the cutoff -> full coverage.
    rows = [(base, f"m{i}", 10.0) for i in range(300)]
    # Give m0 an extra earlier bucket so it is guaranteed inside the top-250
    # LIMIT regardless of tie ordering, and corrupt BOTH of its prices so
    # the cutoff miss falls back to an equally invalid value.
    rows.append((base - 3600, "m0", 10.0))
    _seed(conn, rows)
    conn.execute(
        "UPDATE quote_snapshots SET sell_price = NULL WHERE market_name = 'm0'",
    )
    conn.commit()
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base, old=None)
    # m0's cutoff price is NULL -> it falls back, finds no earlier bucket,
    # and is rejected.  The candidate set (249) is smaller than the LIMIT
    # total (250), and the coverage ratio is measured against that total,
    # so it can never reach 1.0.
    assert result.top_candidate_total == 250
    assert len(result.candidates) == 249
    assert any(r["market_name"] == "m0" for r in result.rejected)
    covered = sum(1 for c in result.candidates if c.price_source == "cutoff-bucket-hit")
    ratio = covered / result.top_candidate_total
    assert ratio < 1.0, "invalid price must reduce coverage, not shrink the denominator"
    conn.close()


def test_boundary_tie_248_plus_3_recorded_and_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tied group straddling the 250th rank is recorded as a boundary
    resolution whose membership is fixed by the name key; an internal tied
    group fully inside the top-N is only reported for audit.

    Fixture: 248 strictly-better names (each with two buckets, forming one
    internal tied pair group) + a tie group of 3 at ranks 249..251.  With
    LIMIT 250 the name key admits tie-0 and tie-1 (tie-2 falls outside),
    which must NOT fail the proposal by itself."""
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "i.db")
    rows = []
    for i in range(248):
        rows.append((base - 3600, f"top{i}", 10.0))
        rows.append((base, f"top{i}", 10.0))
    for i in range(3):
        rows.append((base - 3600, f"tie-{i}", 5.0 + i))
    _seed(conn, rows)
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base, old=None)
    assert len(result.candidates) == tool.TARGET  # LIMIT caps at 250 rows
    # The 248-name (buckets=2, last_bucket=base) group is internal (audit).
    assert any(entry["pair"] == [2, base] for entry in result.ranking_ties_internal)
    resolutions = [
        entry
        for entry in result.boundary_resolutions
        if entry["rank_range"][0] <= tool.TARGET < entry["rank_range"][1]
    ]
    assert len(resolutions) == 1, resolutions
    boundary = resolutions[0]
    assert boundary["full_group_size"] == 3
    assert boundary["all_competing_members"] == ["tie-0", "tie-1", "tie-2"]
    assert boundary["selected_members"] == ["tie-0", "tie-1"]
    assert boundary["not_selected_members"] == ["tie-2"]
    conn.close()


def test_boundary_tie_249_plus_2_resolved_by_name_key_with_full_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """249 strictly-better names + a tie pair at ranks 250..251: LIMIT 250
    shows only ONE member of the tied pair, yet the full group (2) straddles
    the boundary.  The boundary must be detected from the FULL group size,
    never skipped because the LIMIT-visible count is one.  The name key
    picks tie-a (name order) into the top-250, records tie-b as
    not-selected, and the proposal passes (rc=0) because both members have
    valid in-age quotes — a boundary tie alone no longer fails it."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    conn = sqlite3.connect(snapshot)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE quote_snapshots ADD COLUMN data_time INTEGER")
    rows = []
    for i in range(249):
        rows.append((base - 3600, f"top{i}", 10.0))
        rows.append((base, f"top{i}", 10.0))
    # tie pair shares (1 bucket, last=base-3600): ranks 250..251.
    rows.append((base - 3600, "tie-a", 5.0))
    rows.append((base - 3600, "tie-b", 6.0))
    conn.executemany(
        "INSERT INTO quote_snapshots (bucket_at, market_name, sell_price) VALUES (?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(["--cutoff", str(base), "--reference", str(base), "--out-dir", str(out)])
    assert rc == 0, "a name-key-resolved boundary tie must not fail the proposal"
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    resolutions = comp["boundary_resolutions"]
    assert len(resolutions) == 1, resolutions
    boundary = resolutions[0]
    assert boundary["full_group_size"] == 2
    assert sorted(boundary["all_competing_members"]) == ["tie-a", "tie-b"]
    assert boundary["selected_members"] == ["tie-a"]
    assert boundary["not_selected_members"] == ["tie-b"]
    assert boundary["tie_break_key"] == "market_name COLLATE BINARY ASC"
    assert comp["gates"]["all_selected_prices_valid"] is True
    assert comp["overall_pass"] is True
    prop = json.loads((out / "proposed-components.json").read_text(encoding="utf-8"))
    names = {c["market_name"] for c in prop["components"]}
    assert len(names) == tool.TARGET
    assert "tie-a" in names and "tie-b" not in names


def test_main_rejects_unaligned_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-hour-aligned --cutoff must be refused (rc=2) BEFORE any output
    directory is created — never silently floor-aligned and accepted."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    conn, _names = _make_full_db(snapshot, 260, base=base)
    conn.close()
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(
        [
            "--cutoff",
            str(base + 1),  # NOT hour-aligned
            "--reference",
            str(base),
            "--out-dir",
            str(out),
        ]
    )
    assert rc == 2
    assert not out.exists(), "no output directory may be created for a refused cutoff"


def test_main_accepts_non_aligned_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R is a reference INSTANT, not a bucket: only B must be hour-aligned.
    An explicit R = B+1s must proceed into the rehearsal computation."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    conn, _names = _make_full_db(snapshot, 260, base=base)
    conn.close()
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(
        [
            "--cutoff",
            str(base),
            "--reference",
            str(base + 1),  # R need not be hour-aligned
            "--out-dir",
            str(out),
        ]
    )
    assert rc == 0, "non-aligned R must proceed into the rehearsal (rc 0 = pass)"
    sm = json.loads((out / "source-manifest.json").read_text(encoding="utf-8"))
    assert sm["reference_source"] == "explicit-cli"
    from datetime import datetime, timezone

    assert sm["reference_time_utc"] == datetime.fromtimestamp(base + 1, timezone.utc).isoformat()


def test_default_reference_equals_validated_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --reference, R defaults to the VALIDATED cutoff B (an older
    bucket), never the newest bucket.  Proof that cannot pass under the
    wrong R=latest_bucket implementation: the older buckets sit OUTSIDE the
    30-day window anchored at the newest bucket, so under R=newest the
    candidate set would be EMPTY (fails the 250-candidate assertion), while
    under R=B=older the window contains them and exactly 250 candidates
    rank.  Reference time and source are asserted exactly."""
    newest = 200_000 * 3600
    older = newest - 60 * 86400  # 60 days before newest: outside R=newest's window
    snapshot = tmp_path / "snap.db"
    conn = sqlite3.connect(snapshot)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE quote_snapshots ADD COLUMN data_time INTEGER")
    rows: list[tuple[int, str, float]] = []
    for i in range(250):
        rows.append((older, f"m{i}", 10.0 + i % 7))
    # m0..m9 get one extra older bucket so their ranking is strictly above
    # the others; with exactly 250 names no boundary tie exists.
    for i in range(10):
        rows.append((older - 3600, f"m{i}", 11.0 + i))
    # A name ONLY present at the newest bucket (after R) must never rank.
    rows.append((newest, "after-r-only", 99.0))
    conn.executemany(
        "INSERT INTO quote_snapshots (bucket_at, market_name, sell_price) VALUES (?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(["--cutoff", str(older), "--out-dir", str(out)])
    assert rc == 0, "250 deterministic candidates must pass all gates"
    sm = json.loads((out / "source-manifest.json").read_text(encoding="utf-8"))
    from datetime import datetime, timezone

    assert sm["cutoff_bucket"] == older
    assert sm["reference_time_utc"] == datetime.fromtimestamp(older, timezone.utc).isoformat(), (
        "default reference must exactly equal the validated cutoff B"
    )
    assert sm["reference_source"] == "human-designated-default-B"
    prop = json.loads((out / "proposed-components.json").read_text(encoding="utf-8"))
    assert len(prop["components"]) == 250, "250 candidates required, not an empty set"
    assert "after-r-only" not in {c["market_name"] for c in prop["components"]}
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert "after-r-only" not in set(comp["added"])
    assert comp["overall_pass"] is True

    # Controlled counter-proof in memory: with the default R reverted to
    # latest_bucket (newest), the same fixture yields ZERO candidates, so
    # this test must fail under the wrong implementation.  The production
    # file is never modified — only the injectable default is overridden.
    monkeypatch.setattr(
        tool,
        "_default_reference",
        lambda latest_bucket, cutoff: (latest_bucket, "human-designated-default-B"),
    )
    out_wrong = tmp_path / "out-wrong"
    rc_wrong = tool.main(["--cutoff", str(older), "--out-dir", str(out_wrong)])
    assert rc_wrong == 1, "wrong default R (latest_bucket) must not produce a pass"
    prop_wrong = json.loads((out_wrong / "proposed-components.json").read_text(encoding="utf-8"))
    assert prop_wrong["count"] == 0, "window anchored at newest excludes the older buckets"


def _make_full_db(path: Path, count: int, *, base: int) -> tuple[sqlite3.Connection, list[str]]:
    """A snapshot-shaped DB with `count` names (data_time column included).
    Bucket counts strictly DESCEND with the name index (m0 has the most
    buckets, m{count-1} one), so m0..m{count-11} rank inside the top-250
    deterministically and the highest names never tie at the boundary."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE quote_snapshots ADD COLUMN data_time INTEGER")
    names = [f"m{i}" for i in range(count)]
    rows: list[tuple[int, str, float]] = []
    for i, name in enumerate(names):
        bucket_count = count - i  # m0 -> count buckets … last -> 1
        for b in range(1, bucket_count + 1):  # newest bucket IS `base`
            rows.append((base - (bucket_count - b) * 3600, name, 10.0 + i % 7))
    _seed(conn, rows)
    return conn, names


def _synthetic_manifest(tmp_path: Path, snapshot: Path) -> Path:
    manifest = tmp_path / "backup-manifest.json"
    manifest.write_text(
        json.dumps({"databases": [{"name": "market_history", "sha256": tool.sha256_of(snapshot)}]}),
        encoding="utf-8",
    )
    return manifest


def _aged_m0_db(path: Path, base: int, gap_seconds: int) -> None:
    """A snapshot-shaped DB with 300 names where m0 (the highest-ranked
    name) only has quotes starting ``gap_seconds`` before the cutoff.  All
    other names are priced at the cutoff, so 250 members are selected and
    m250..m299 exist as lower-ranked spare candidates that must never be
    pulled in when m0's quote fails the age gate."""
    conn, _names = _make_full_db(path, 300, base=base)
    conn.execute("DELETE FROM quote_snapshots WHERE market_name = 'm0'")
    for k in range(251):  # keep m0 near the top of the ranking
        conn.execute(
            "INSERT INTO quote_snapshots (bucket_at, market_name, sell_price) "
            "VALUES (?, 'm0', 5.0)",
            (base - gap_seconds - k * 3600,),
        )
    conn.commit()
    conn.close()


def test_selection_invariant_to_insert_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same data seeded in opposite insertion orders must yield exactly
    the same selected list.  The boundary pair tie-a/tie-b straddles the
    250th rank, so an ordering that leaked DB row order would pick different
    members per insertion order."""
    base = 100_000 * 3600

    def rows_builder() -> list[tuple[int, str, float]]:
        rows: list[tuple[int, str, float]] = []
        for i in range(249):
            rows.append((base - 3600, f"top{i}", 10.0))
            rows.append((base, f"top{i}", 10.0))
        rows.append((base - 3600, "tie-a", 5.0))
        rows.append((base - 3600, "tie-b", 6.0))
        return rows

    conn_forward = _make_db(tmp_path / "ord-fwd.db")
    _seed(conn_forward, rows_builder())
    conn_backward = _make_db(tmp_path / "ord-bwd.db")
    _seed(conn_backward, list(reversed(rows_builder())))
    monkeypatch.setattr(tool, "open_ro", lambda path: conn_forward)
    forward = tool.compute(conn_forward, cutoff=base, reference=base, old=None)
    backward = tool.compute(conn_backward, cutoff=base, reference=base, old=None)
    names_forward = [c.market_name for c in forward.candidates]
    names_backward = [c.market_name for c in backward.candidates]
    assert names_forward == names_backward
    assert len(names_forward) == tool.TARGET
    assert "tie-a" in names_forward and "tie-b" not in names_forward
    conn_forward.close()
    conn_backward.close()


def test_fallback_quote_age_gate_exact_24h_and_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fallback quote exactly 24h before B (age 86400s) is allowed; any
    fallback older than that — even by one second — is rejected as an
    aged fallback.  Per-member quote age is recorded on accepted candidates
    and on the rejection reason."""
    base = 100_000 * 3600
    conn = _make_db(tmp_path / "age.db")
    rows: list[tuple[int, str, float]] = []
    for i in range(247):
        rows.append((base - 3600, f"top{i}", 10.0))
        rows.append((base, f"top{i}", 10.0))
    rows.append((base - 86400, "fa-exact-24h", 7.0))  # age 86400 -> allowed
    rows.append((base - 86401, "fa-over-1s", 8.0))  # age 86401 -> refused
    rows.append((base - 90000, "fa-over-25h", 9.0))  # age 90000 -> refused
    _seed(conn, rows)
    monkeypatch.setattr(tool, "open_ro", lambda path: conn)
    result = tool.compute(conn, cutoff=base, reference=base, old=None)
    by_name = _candidates_named(result)
    assert by_name["fa-exact-24h"].price_source == "item-fallback"
    assert by_name["fa-exact-24h"].quote_age_seconds == 86400
    assert by_name["fa-exact-24h"].base_price == 7.0
    rejects = {r["market_name"]: r for r in result.rejected}
    assert "fa-exact-24h" not in rejects
    assert rejects["fa-over-1s"]["class"] == "aged-fallback"
    assert rejects["fa-over-1s"]["quote_age_seconds"] == 86401
    assert rejects["fa-over-25h"]["class"] == "aged-fallback"
    assert rejects["fa-over-25h"]["quote_age_seconds"] == 90000
    conn.close()


def test_aged_fallback_fails_whole_with_spare_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m0 is selected (rank ~50) but its only quotes are 25h old: the aged
    fallback rejects the member and the whole proposal fails even though
    m250..m299 exist as spare candidates and the coverage ratio alone would
    still pass.  No refill, no lowered bar, original denominator kept."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    _aged_m0_db(snapshot, base, gap_seconds=90000)  # 25h > 24h gate
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(["--cutoff", str(base), "--reference", str(base), "--out-dir", str(out)])
    assert rc == 1, "a selected member with an aged fallback quote must fail the proposal"
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert comp["proposed_count"] == 249
    assert comp["gates"]["invalid_selected_count"] == 1
    assert comp["gates"]["aged_fallback_selected_count"] == 1
    assert comp["gates"]["coverage_ratio"] >= 0.90  # ratio alone would pass
    assert comp["overall_pass"] is False
    assert [r["market_name"] for r in comp["rejected"]] == ["m0"]
    assert comp["rejected"][0]["class"] == "aged-fallback"
    prop = json.loads((out / "proposed-components.json").read_text(encoding="utf-8"))
    names = {c["market_name"] for c in prop["components"]}
    assert "m250" not in names, "spare candidates must never be pulled in"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "rehearsal: FAIL" in report


def test_exact_24h_fallback_passes_same_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling of the aged test: the identical fixture with m0's quotes at
    exactly 24h (86400s) instead of 25h passes every gate (rc=0), pinning
    the age threshold precisely at 86400s."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    _aged_m0_db(snapshot, base, gap_seconds=86400)  # exactly 24h -> allowed
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(["--cutoff", str(base), "--reference", str(base), "--out-dir", str(out)])
    assert rc == 0, "a fallback exactly 24h old must pass the age gate"
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert comp["proposed_count"] == 250
    assert comp["gates"]["all_selected_prices_valid"] is True
    assert comp["overall_pass"] is True


def test_main_success_path_writes_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real main() end-to-end: authorized snapshot, aligned existing B <= R,
    enough candidates -> PASS, exit 0, five output files written with the
    tool's own sha and reference source recorded."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    conn, _names = _make_full_db(snapshot, 260, base=base)
    conn.close()
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(["--cutoff", str(base), "--reference", str(base), "--out-dir", str(out)])
    assert rc == 0
    for name in (
        "source-manifest.json",
        "old-components.json",
        "proposed-components.json",
        "comparison.json",
        "report.md",
    ):
        assert (out / name).is_file(), f"missing output {name}"
    sm = json.loads((out / "source-manifest.json").read_text(encoding="utf-8"))
    assert sm["snapshot_sha256"] == tool.sha256_of(snapshot)
    assert sm["tool_sha256"]
    assert sm["reference_source"] in {"explicit-cli", "human-designated-default-B"}
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert comp["overall_pass"] is True


def test_main_refuses_unauthorized_snapshot_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot whose sha is NOT in the manifest is refused BEFORE any DB
    query or output creation."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    conn, _names = _make_full_db(snapshot, 260, base=base)
    conn.close()
    manifest = tmp_path / "backup-manifest.json"
    manifest.write_text(
        json.dumps({"databases": [{"name": "market_history", "sha256": "0" * 64}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    opened: list[object] = []

    def spy_open(path: Path) -> sqlite3.Connection:
        opened.append(path)
        raise AssertionError("DB must not be opened when the hash check fails")

    monkeypatch.setattr(tool, "open_ro", spy_open)
    out = tmp_path / "out"
    rc = tool.main(["--cutoff", str(base), "--reference", str(base), "--out-dir", str(out)])
    assert rc == 2
    assert opened == [], "open_ro must not run before the manifest check"
    assert not out.exists()


def test_main_rejects_b_after_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    conn, _names = _make_full_db(snapshot, 260, base=base)
    conn.close()
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    rc = tool.main(
        [
            "--cutoff",
            str(base),
            "--reference",
            str(base - 3600),
            "--out-dir",
            str(tmp_path / "o"),
        ]
    )
    assert rc == 2  # B after R is refused


def test_main_gate_fails_with_250_selected_249_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """250 selected, 249 valid, 1 invalid base price: coverage ratio stays
    above 90% yet the proposal must fail overall (no auto-fill), and JSON /
    Markdown / exit status agree."""
    base = 100_000 * 3600
    snapshot = tmp_path / "snap.db"
    conn, _names = _make_full_db(snapshot, 260, base=base)
    # m0 has the most buckets (260) so it ranks first and is selected;
    # null out ALL of its prices so it is rejected (no auto-fill).
    conn.execute("UPDATE quote_snapshots SET sell_price = NULL WHERE market_name = 'm0'")
    conn.commit()
    conn.close()
    manifest = _synthetic_manifest(tmp_path, snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SNAPSHOT_MANIFEST", manifest)
    monkeypatch.setattr(tool, "OUT_ROOT", tmp_path)
    out = tmp_path / "out"
    rc = tool.main(["--cutoff", str(base), "--reference", str(base), "--out-dir", str(out)])
    assert rc == 1, "a selected member with an invalid base price must fail the proposal"
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert comp["gates"]["invalid_selected_count"] >= 1
    assert comp["gates"]["all_selected_prices_valid"] is False
    assert comp["overall_pass"] is False
    assert comp["gates"]["coverage_ratio"] >= 0.90  # ratio alone would pass
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "rehearsal: FAIL" in report
