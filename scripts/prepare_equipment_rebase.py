#!/usr/bin/env python
"""Prepare-equipment-rebase dry-run tool (authorized scope: snapshot only).

Reads ONLY the approved backup snapshot
(``data/backups/equipment-rebase/market_history.db``) through a
``mode=ro`` SQLite URI and computes what a same-month equipment-index
rebuild *would* look like if it were executed against a fresh snapshot.

Capabilities: ``prepare`` / ``dry-run`` only.  There is deliberately NO
``--apply`` / ``--force`` / write path, and this module never imports the
production store/service entry points (which would auto-initialize or
start business tasks).  All SQL reads are parameterised; nothing in this
file is a credential or a chat/business record.

Rule source (single source of truth: src/services/equipment_rules.py,
imported by BOTH this tool and the production path in market_history.py):
- candidate ranking = hourly-bucket count over the trailing 30 days before
  the cutoff, ties broken by latest bucket, THEN by market name (BINARY),
  top ``target`` items.  The name key makes selection a deterministic total
  order.  Production ``market_history.py`` ranks with the same shared
  clause.
- membership is decided FIRST (full sort, LIMIT target), prices validated
  AFTER; an invalid or aged price never pulls in a lower-ranked candidate.
- base price = sell price at the fixed cutoff bucket B when present,
  otherwise fall back to the item's most recent price at-or-before B; a
  fallback quote must satisfy 0 <= B - source_bucket <= 86400s and be a
  finite positive number, otherwise the selected member is rejected;
- gates (equipment_index.py / market_history.py): >= MIN_COMPONENTS(200)
  components, >= 200 covered at B, coverage >= 0.90, all prices positive,
  and no selected member rejected (invalid or aged quote).  A rejection
  never shrinks the denominator (selected total) nor lowers the coverage
  bar; the proposal simply fails.
- index = equal-weight log index exp(mean(log(latest/base))) * 100; with a
  fixed input price equal to base, the value is ~100 by construction.
- freshness: the implementation rule 0 <= T - B <= 7200s (T = effective
  time) is enforced by the internal transaction function
  (src/services/equipment_rebase_txn.py) against the REAL clock; this
  rehearsal tool keeps T undecided and documents the rule only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The deterministic ordering and fallback-age rules are shared with the
# production path (src/services/equipment_rules.py): one source of truth so
# the rehearsal and the production refresh cannot drift apart.  The shared
# module is pure constants — importing it never initialises a store.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from src.services.equipment_rules import (  # noqa: E402  (path bootstrap above)
    EQUIPMENT_FALLBACK_MAX_AGE_SECONDS,
    EQUIPMENT_ORDER_BY,
)

FALLBACK_MAX_AGE_SECONDS = EQUIPMENT_FALLBACK_MAX_AGE_SECONDS
ORDERING_CLAUSE = EQUIPMENT_ORDER_BY

TOOL_VERSION = "prepare-equipment-rebase-0.5.0"
SNAPSHOT = _PROJECT_ROOT / "data/backups/equipment-rebase/market_history.db"
SNAPSHOT_MANIFEST = SNAPSHOT.with_name("backup-manifest.json")
OUT_ROOT = _PROJECT_ROOT / "data/equipment-rebase-dryrun"
TARGET = 250
MIN_COMPONENTS = 200
MIN_COVERAGE_RATIO = 0.90
# Fallback-quote age gate (rehearsal rule v0.5.0): a fallback price must
# come from a bucket no older than 24h before the cutoff B.  The value lives
# in src/services/equipment_rules.py (shared with production).
ORDERING_NOTE = (
    "Rehearsal candidate ordering is deterministic: "
    + ORDERING_CLAUSE
    + ". Production src/services/market_history.py now ranks with the SAME "
    "shared clause (equipment_rules.EQUIPMENT_ORDER_BY); this rehearsal "
    "still reports (buckets, last_bucket) tie pairs for audit."
)
# Arbitrary run id for a unique output directory.
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Candidate:
    market_name: str
    bucket_count: int
    last_bucket: int
    base_price: float | None
    price_source: str  # "cutoff-bucket-hit" | "item-fallback"
    source_bucket: int
    quote_age_seconds: int  # cutoff - source_bucket, in seconds (0 for a hit)
    quote_age_buckets: int  # cutoff - source_bucket, in whole buckets
    old_price: float | None = None  # price in the old component table


@dataclass
class Result:
    cutoff: int
    reference_time: int | None  # snapshot wall-clock instant; None when unknown
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[dict[str, object]] = field(default_factory=list)
    # Total rows the production query would have returned before price
    # validation (the LIMIT cap).  Coverage denominators must use this
    # number so an invalid price cannot inflate the ratio by shrinking the
    # denominator.
    top_candidate_total: int = 0
    # Tie handling: production ORDER BY buckets DESC, last_bucket DESC is a
    # total order only when (buckets, last_bucket) is unique.  Pairs with
    # more than one member are reported for audit.  Membership itself is
    # ALWAYS deterministic here because the rehearsal ordering appends the
    # third key market_name COLLATE BINARY ASC.
    ranking_ties_internal: list[dict[str, object]] = field(default_factory=list)
    # A tied group straddling the Nth boundary.  Under the third key its
    # members are uniquely ordered; the record lists the full competing
    # group, who was selected (rank <= TARGET) and who was not.  A boundary
    # tie no longer fails the proposal by itself.
    boundary_resolutions: list[dict[str, object]] = field(default_factory=list)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"snapshot not found: {path}")
    # mode=ro as mandated; never a plain connect + query_only.
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def read_old_components(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    rows = conn.execute(
        "SELECT market_name, base_day, base_price, added_at FROM equipment_index_components"
    ).fetchall()
    return {
        str(r[0]): {
            "market_name": str(r[0]),
            "base_day": str(r[1]),
            "base_price": float(r[2]) if r[2] is not None else None,
            "added_at": int(r[3]) if r[3] is not None else None,
        }
        for r in rows
    }


def _default_reference(latest_bucket: int, cutoff: int) -> tuple[int, str]:
    """Default rehearsal reference R when --reference is absent.

    Production decision: R = the validated cutoff B (latest_bucket is
    passed for context but deliberately unused).  Tests may override this
    function to prove the wrong decision (R=latest_bucket) fails.
    """
    del latest_bucket  # context only; the decision is R=cutoff
    return cutoff, "human-designated-default-B"


def compute(
    conn: sqlite3.Connection,
    cutoff: int,
    reference: int,
    old: dict[str, dict[str, object]] | None = None,
) -> Result:
    """Rank candidates and price them at the fixed cutoff bucket B.

    ``cutoff`` (B) is the hour-aligned bucket whose real prices become the
    proposed base and the index input.  ``reference`` (R) is the rehearsal
    reference instant: the trailing 30-day ranking window runs from
    ``R - 30d``, while quote selection never uses data after B.
    """
    since = reference - 30 * 86400
    # Selection first: the full deterministic ordering (buckets DESC,
    # last_bucket DESC, market_name COLLATE BINARY ASC) fixes membership
    # BEFORE any price validation.  Invalid or aged quotes below never pull
    # in a lower-ranked candidate.
    rows = conn.execute(
        "SELECT market_name, COUNT(*) AS buckets, MAX(bucket_at) AS last_bucket "
        "FROM quote_snapshots WHERE bucket_at >= ? AND bucket_at <= ? "
        f"GROUP BY market_name {ORDERING_CLAUSE} LIMIT ?",
        (since, cutoff, TARGET),
    ).fetchall()
    result = Result(cutoff=cutoff, reference_time=None, top_candidate_total=len(rows))

    # Ranking ties: group by (buckets, last_bucket) and report each group
    # for audit.  The rehearsal ordering's third key (market_name COLLATE
    # BINARY ASC) decides membership deterministically — nothing here is
    # left to DB row order.
    groups: dict[tuple[int, int], list[tuple[str, int, int]]] = {}
    for r in rows:
        key = (int(r[1]), int(r[2]))
        groups.setdefault(key, []).append((str(r[0]), int(r[1]), int(r[2])))

    # For each tied key present in the top-N, learn the FULL group size from
    # the whole table: the boundary test needs the group's complete rank
    # span, not just the members that happened to fit under LIMIT.
    full_group_sizes: dict[tuple[int, int], int] = {}
    for key in groups:
        size_row = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT market_name, COUNT(*) AS buckets, MAX(bucket_at) AS last_bucket "
            "  FROM quote_snapshots WHERE bucket_at >= ? AND bucket_at <= ? "
            "  GROUP BY market_name "
            ") t WHERE t.buckets = ? AND t.last_bucket = ?",
            (since, cutoff, key[0], key[1]),
        ).fetchone()
        full_group_sizes[key] = int(size_row[0]) if size_row else 0

    for key, members in groups.items():
        names = [m[0] for m in members]
        group_total = full_group_sizes.get(key, len(names))
        # A tie only exists when the FULL group (across the whole table)
        # has more than one member.  Membership inside the top-N may show a
        # single LIMIT-visible member while the full group straddles the
        # boundary — skipping on the visible count would lose that case.
        if group_total <= 1:
            continue
        # Determine whether this tied group straddles the boundary: count
        # rows with a strictly-better (buckets, last_bucket) rank across the
        # whole table.  A group is internal when it is entirely within the
        # top N; otherwise it is a boundary tie.
        strictly_better = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT market_name, COUNT(*) AS buckets, MAX(bucket_at) AS last_bucket "
            "  FROM quote_snapshots WHERE bucket_at >= ? AND bucket_at <= ? "
            "  GROUP BY market_name "
            ") t WHERE (t.buckets > ?) OR (t.buckets = ? AND t.last_bucket > ?)",
            (since, cutoff, key[0], key[0], key[1]),
        ).fetchone()
        better_count = int(strictly_better[0]) if strictly_better else 0
        position_start = better_count + 1  # first member's rank (1-based)
        position_end = better_count + group_total
        entry = {
            "pair": [key[0], key[1]],
            "names": sorted(names),
            "full_group_size": group_total,
            "rank_range": [position_start, position_end],
        }
        if position_start <= TARGET < position_end:
            # Boundary group.  Fetch ALL competing members (including those
            # outside the LIMIT window) ordered by the third key, then split
            # into selected (rank <= TARGET) and not-selected.  The split
            # must match the SQL LIMIT exactly: the first (TARGET -
            # better_count) members in name order enter the top-N.
            all_members = conn.execute(
                "SELECT market_name FROM ("
                "  SELECT market_name, COUNT(*) AS buckets, MAX(bucket_at) AS last_bucket "
                "  FROM quote_snapshots WHERE bucket_at >= ? AND bucket_at <= ? "
                "  GROUP BY market_name "
                ") t WHERE t.buckets = ? AND t.last_bucket = ? "
                "ORDER BY t.market_name COLLATE BINARY ASC",
                (since, cutoff, key[0], key[1]),
            ).fetchall()
            ordered_members = [str(r[0]) for r in all_members]
            selected_count = max(0, min(group_total, TARGET - better_count))
            selected = ordered_members[:selected_count]
            not_selected = ordered_members[selected_count:]
            # The LIMIT-visible group members seen by the ranking query must
            # equal the name-keyed selected set; assert parity so a future
            # ordering change cannot silently desync the audit record.
            limit_visible = sorted(m[0] for m in members)
            assert set(selected) == set(limit_visible), (
                f"boundary split mismatch for pair {key}: selected {selected} "
                f"!= limit-visible {limit_visible}"
            )
            entry["all_competing_members"] = ordered_members
            entry["tie_break_key"] = "market_name COLLATE BINARY ASC"
            entry["selected_members"] = selected
            entry["not_selected_members"] = not_selected
            result.boundary_resolutions.append(entry)
        else:
            result.ranking_ties_internal.append(entry)

    for r in rows:
        name, buckets, last_bucket = str(r[0]), int(r[1]), int(r[2])
        old_price = None
        if old is not None and name in old:
            old_price_raw = old[name].get("base_price")
            if old_price_raw is not None:
                try:
                    old_price = float(str(old_price_raw))
                except (TypeError, ValueError):
                    old_price = None
        price_row = conn.execute(
            "SELECT sell_price FROM quote_snapshots WHERE market_name = ? AND bucket_at = ?",
            (name, cutoff),
        ).fetchone()
        if price_row is not None and price_row[0] is not None:
            source_bucket, source = cutoff, "cutoff-bucket-hit"
            age_seconds = 0
        else:
            # Item's most recent price at-or-before the cutoff.  A fallback
            # quote must be fresh enough: 0 <= B - source_bucket <= 24h.
            recent = conn.execute(
                "SELECT MAX(bucket_at) FROM quote_snapshots "
                "WHERE market_name = ? AND bucket_at <= ?",
                (name, cutoff),
            ).fetchone()
            if recent is None or recent[0] is None:
                result.rejected.append(
                    {
                        "market_name": name,
                        "class": "missing-price",
                        "reason": "no price at-or-before cutoff",
                    }
                )
                continue
            source_bucket = int(recent[0])
            source = "item-fallback"
            age_seconds = cutoff - source_bucket
            if age_seconds < 0 or age_seconds > FALLBACK_MAX_AGE_SECONDS:
                result.rejected.append(
                    {
                        "market_name": name,
                        "class": "aged-fallback",
                        "source_bucket": source_bucket,
                        "quote_age_seconds": age_seconds,
                        "reason": (
                            f"fallback quote age {age_seconds}s exceeds "
                            f"{FALLBACK_MAX_AGE_SECONDS}s (24h) gate"
                        ),
                    }
                )
                continue
            price_row = conn.execute(
                "SELECT sell_price FROM quote_snapshots WHERE market_name = ? AND bucket_at = ?",
                (name, source_bucket),
            ).fetchone()
            if price_row is None or price_row[0] is None:
                result.rejected.append(
                    {
                        "market_name": name,
                        "class": "missing-price",
                        "source_bucket": source_bucket,
                        "quote_age_seconds": age_seconds,
                        "reason": "fallback bucket row missing",
                    }
                )
                continue
        price = float(price_row[0])
        if not math.isfinite(price) or price <= 0:
            result.rejected.append(
                {
                    "market_name": name,
                    "class": "invalid-price",
                    "source_bucket": source_bucket,
                    "quote_age_seconds": age_seconds,
                    "reason": f"non-positive price: {price!r}",
                }
            )
            continue
        result.candidates.append(
            Candidate(
                market_name=name,
                bucket_count=buckets,
                last_bucket=last_bucket,
                base_price=price,
                price_source=source,
                source_bucket=source_bucket,
                quote_age_seconds=age_seconds,
                quote_age_buckets=max(0, age_seconds // 3600),
                old_price=old_price,
            )
        )
    return result


def index_value(candidates: list[Candidate], price_map: dict[str, float]) -> float | None:
    """Equal-weight log index exp(mean(log(price/base))) * 100.

    ``price_map`` carries the REAL cutoff-bucket (or fallback) price per
    name — not the proposed base.  Indexing a fixed input price equal to
    each candidate's base yields ~100; different real prices yield a
    genuine non-100 reading.  The mean runs over the candidates that have
    a positive price in ``price_map``.
    """
    logs: list[float] = []
    for c in candidates:
        price = price_map.get(c.market_name)
        if price is None or price <= 0 or c.base_price is None or c.base_price <= 0:
            continue
        logs.append(math.log(price / c.base_price))
    if len(logs) < MIN_COMPONENTS:
        return None
    return math.exp(sum(logs) / len(logs)) * 100.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cutoff",
        type=int,
        default=None,
        help="cutoff bucket B (hour-aligned Unix seconds); default = snapshot MAX(bucket_at)",
    )
    parser.add_argument(
        "--reference",
        type=int,
        default=None,
        help="rehearsal reference instant R (Unix seconds) for the 30-day ranking "
        "window; quotes never use data after B.  Explicitly a human-designated "
        "rehearsal parameter — no historical meaning is claimed.  "
        "Default = the validated cutoff B.",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="override the snapshot database path (default: the approved "
        "pre-release-20260907-online copy); the manifest hash gate still applies",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="override the backup-manifest.json path paired with --snapshot",
    )
    parser.add_argument("--out-dir", default=None, help="override output directory")
    args = parser.parse_args(argv)
    snapshot_path = Path(args.snapshot) if args.snapshot else SNAPSHOT
    manifest_path = Path(args.manifest) if args.manifest else SNAPSHOT_MANIFEST

    out_dir = Path(args.out_dir) if args.out_dir else OUT_ROOT / RUN_ID
    if out_dir.exists():
        print(f"REFUSED: output directory already exists: {out_dir}")
        return 2
    # Output must stay under the dry-run root: an explicit out-dir escaping
    # OUT_ROOT (or pointing at the snapshot dir) is refused.
    if args.out_dir is not None:
        try:
            out_dir.resolve().relative_to(OUT_ROOT.resolve())
        except ValueError:
            print(f"REFUSED: --out-dir escapes the dry-run root: {out_dir}")
            return 2

    # 1) Snapshot manifest check BEFORE any DB query or output creation.
    manifest_sha = sha256_of(snapshot_path)
    authorized: list[str] = []
    if not manifest_path.is_file():
        print(f"REFUSED: snapshot manifest missing: {manifest_path}")
        return 2
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    for db in manifest.get("databases", []):
        if db.get("name") == "market_history" and db.get("sha256"):
            authorized.append(str(db["sha256"]))
    if manifest_sha not in authorized:
        print(
            "REFUSED: snapshot hash not authorized by backup-manifest.json "
            f"(snapshot {manifest_sha[:16]}… not in manifest)"
        )
        return 2

    conn = open_ro(snapshot_path)
    try:
        # 2) Resolve B: the caller-supplied cutoff must be hour-aligned and
        # actually exist; it is NEVER silently floor-aligned.  When absent,
        # B defaults to the snapshot's newest bucket.
        row = conn.execute("SELECT MAX(bucket_at) FROM quote_snapshots").fetchone()
        latest_bucket = int(row[0]) if row and row[0] is not None else 0
        if args.cutoff is not None and args.cutoff % 3600 != 0:
            print(
                f"REFUSED: cutoff bucket B={args.cutoff} is not hour-aligned "
                "(hour-aligned bucket required)"
            )
            return 2
        cutoff = args.cutoff if args.cutoff is not None else latest_bucket
        exists_row = conn.execute(
            "SELECT COUNT(*) FROM quote_snapshots WHERE bucket_at = ?", (cutoff,)
        ).fetchone()
        exists = int(exists_row[0]) > 0 if exists_row else False
        # 3) Resolve R: explicit rehearsal reference; never derived from an
        # arbitrary product's data_time.  When R is not passed it defaults
        # to the ALREADY-validated cutoff B, so the ranking window and the
        # recorded reference always agree.
        if args.reference is not None:
            reference = int(args.reference)
            reference_source = "explicit-cli"
        else:
            reference, reference_source = _default_reference(latest_bucket, cutoff)
        if not exists:
            print(f"REFUSED: cutoff bucket B={cutoff} does not exist in the snapshot")
            return 2
        if cutoff > reference:
            print(
                f"REFUSED: cutoff bucket B={cutoff} is after reference R={reference} "
                "(quotes must never come from after R)"
            )
            return 2
        old = read_old_components(conn)
        result = compute(conn, cutoff=cutoff, reference=reference, old=old)
        result.reference_time = reference
        # 4) Independent real-price map for bucket B only, read from the DB
        # while the connection is open: the index covers exactly the
        # components whose bucket-B quote exists (a fallback price is never
        # treated as a bucket-B quote).
        bucket_b_prices: dict[str, float] = {}
        price_rows = conn.execute(
            "SELECT market_name, sell_price FROM quote_snapshots WHERE bucket_at = ?",
            (cutoff,),
        ).fetchall()
        for price_row in price_rows:
            if price_row[1] is None:
                continue
            try:
                value = float(price_row[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                bucket_b_prices[str(price_row[0])] = value
    finally:
        conn.close()

    out_dir.mkdir(parents=True)
    # resolve exactly once; OUT_ROOT may be relative in tests
    out_dir = out_dir.resolve()

    # Gates — denominators never shrink: coverage and ratio are measured
    # against the full selected total (the LIMIT cap).  Any selected member
    # rejected for an invalid price or an aged fallback quote fails the
    # whole proposal (no refill, no lower bar).
    selected_total = result.top_candidate_total
    covered = sum(1 for c in result.candidates if c.price_source == "cutoff-bucket-hit")
    fallback = sum(1 for c in result.candidates if c.price_source == "item-fallback")
    invalid_selected = len(result.rejected)
    aged_selected = sum(1 for r in result.rejected if r.get("class") == "aged-fallback")
    ratio = covered / selected_total if selected_total else 0.0
    gate_components = selected_total >= MIN_COMPONENTS
    gate_covered = covered >= MIN_COMPONENTS
    gate_ratio = ratio >= MIN_COVERAGE_RATIO
    gate_no_invalid = invalid_selected == 0
    gate_prices = all(c.base_price is not None and c.base_price > 0 for c in result.candidates)
    fixed_input = index_value(result.candidates, bucket_b_prices)
    gate_index_about_100 = fixed_input is not None and abs(fixed_input - 100.0) < 1e-9
    # Boundary ties are resolved by the name key (deterministic), so they no
    # longer fail the proposal by themselves; the resolution is recorded in
    # comparison.json for audit.
    gate_overall = (
        gate_components
        and gate_covered
        and gate_ratio
        and gate_no_invalid
        and gate_prices
        and gate_index_about_100
    )

    added = [c.market_name for c in result.candidates if c.market_name not in old]
    removed = [name for name in old if name not in {c.market_name for c in result.candidates}]
    # Per-item old vs proposed deltas (real old prices; None when the name
    # is new or the old row had no price).
    deltas = []
    for c in sorted(result.candidates, key=lambda c: c.market_name):
        deltas.append(
            {
                "market_name": c.market_name,
                "old_base_price": c.old_price,
                "proposed_base_price": c.base_price,
                "delta": (
                    round(c.base_price - c.old_price, 6)
                    if c.old_price is not None and c.base_price is not None
                    else None
                ),
                "source_bucket": c.source_bucket,
                "price_source": c.price_source,
                "quote_age_seconds": c.quote_age_seconds,
                "quote_age_buckets": c.quote_age_buckets,
            }
        )
    # Output materials (no credentials / chat / unrelated business data).
    tool_sha = sha256_of(Path(__file__))
    (out_dir / "source-manifest.json").write_text(
        json.dumps(
            {
                "snapshot_path": str(snapshot_path.resolve()),
                "snapshot_sha256": manifest_sha,
                "tool": TOOL_VERSION,
                "tool_sha256": tool_sha,
                "reference_time_utc": (
                    datetime.fromtimestamp(result.reference_time, timezone.utc).isoformat()
                    if result.reference_time is not None
                    else None
                ),
                "reference_source": reference_source,
                "reference_note": (
                    "R is an explicitly human-designated rehearsal parameter; "
                    "no historical meaning is claimed when derived as default-B"
                ),
                "cutoff_bucket": result.cutoff,
                "cutoff_bucket_utc": datetime.fromtimestamp(
                    result.cutoff, timezone.utc
                ).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "old-components.json").write_text(
        json.dumps(
            {"count": len(old), "components": sorted(old.values(), key=lambda c: c["market_name"])},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "proposed-components.json").write_text(
        json.dumps(
            {
                "dry_run": True,
                "note": "演练材料：不填造真实生效时间或实际 added_at",
                "effective_time": None,
                "cutoff_bucket": result.cutoff,
                "reference_time_utc": (
                    datetime.fromtimestamp(result.reference_time, timezone.utc).isoformat()
                    if result.reference_time is not None
                    else None
                ),
                "count": len(result.candidates),
                "components": deltas,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "comparison.json").write_text(
        json.dumps(
            {
                "old_count": len(old),
                "proposed_count": len(result.candidates),
                "added": sorted(added),
                "removed": sorted(removed),
                "covered_at_cutoff": covered,
                "fallback": fallback,
                "rejected": result.rejected,
                "ranking_ties_internal": result.ranking_ties_internal,
                "boundary_resolutions": result.boundary_resolutions,
                "ordering": ORDERING_NOTE,
                "fallback_age_gate_seconds": FALLBACK_MAX_AGE_SECONDS,
                "gates": {
                    "components_ge_200": gate_components,
                    "covered_ge_200": gate_covered,
                    "coverage_ratio_ge_0_90": gate_ratio,
                    "coverage_ratio": round(ratio, 4),
                    "all_selected_prices_valid": gate_no_invalid,
                    "invalid_selected_count": invalid_selected,
                    "aged_fallback_selected_count": aged_selected,
                    "all_prices_positive": gate_prices,
                    "fixed_input_index_about_100": gate_index_about_100,
                    "fixed_input_index": fixed_input,
                },
                "overall_pass": gate_overall,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report = (
        "# Equipment rebase dry-run report\n\n"
        f"- tool: {TOOL_VERSION} (sha256 {tool_sha[:16]}…)\n"
        f"- snapshot: {snapshot_path.resolve()} (sha256 {manifest_sha[:16]}…, "
        f"authorized by backup-manifest.json)\n"
        f"- reference R: {result.reference_time or 'unknown'} "
        f"(source: {reference_source}; human-designated rehearsal parameter)\n"
        f"- cutoff bucket B: {result.cutoff} "
        f"({datetime.fromtimestamp(result.cutoff, timezone.utc).isoformat()} UTC)\n"
        f"- candidate ordering: {ORDERING_CLAUSE} (shared rule — production "
        f"market_history.py ranks with the same clause)\n"
        f"- fallback quote age gate: 0 <= B - source_bucket <= "
        f"{FALLBACK_MAX_AGE_SECONDS}s (24h); older fallbacks reject the member\n"
        f"- candidates: {len(result.candidates)}; covered at B: {covered}; "
        f"fallback: {fallback}; coverage {ratio:.2%} (denominator = selected total)\n"
        f"- gates: components≥200 {gate_components}, covered≥200 {gate_covered}, "
        f"ratio≥90% {gate_ratio}, no invalid/aged selected {gate_no_invalid}, "
        f"prices positive {gate_prices}, fixed-input index≈100 {gate_index_about_100}\n"
        f"- fixed-input index value: {fixed_input}\n"
        f"- rejected: {invalid_selected} (aged fallback {aged_selected}, "
        f"per-member age and reason in comparison.json / proposed-components.json)\n"
        f"- ranking ties: internal {len(result.ranking_ties_internal)} for audit; "
        f"boundary {len(result.boundary_resolutions)} resolved by the name key "
        f"(full competing set + selected + not-selected in comparison.json)\n"
        f"- A. algorithm rehearsal: {'PASS' if gate_overall else 'FAIL'}\n"
        f"- B. snapshot freshness for a future real execution: NOT verified.  "
        f"The rehearsal snapshot is not a freshness proof; implementation "
        f"freshness (0 <= T - B <= 7200s with T undecided) is planned in "
        f"docs/releases/EQUIPMENT_REBASE_PLAN.md only, so a rehearsal pass "
        f"does NOT make the index releasable now\n"
        f"- C. production execution authorized: NO\n"
        f"- limitations: snapshot reflects one moment; ordering difference vs "
        f"production documented in comparison.json 'ordering'.\n"
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    print(f"dry-run outputs written to: {out_dir}")
    print(
        f"candidates={len(result.candidates)} covered={covered} fallback={fallback} "
        f"ratio={ratio:.2%} internal_ties={len(result.ranking_ties_internal)} "
        f"boundary_resolved={len(result.boundary_resolutions)} "
        f"rejected={invalid_selected} (aged={aged_selected}) fixed_index={fixed_input} "
        f"overall={'PASS' if gate_overall else 'FAIL'}"
    )
    return 0 if gate_overall else 1


if __name__ == "__main__":
    sys.exit(main())
