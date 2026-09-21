"""Internal equipment-index rebase transaction capabilities.

There is deliberately NO ``--apply`` / CLI / HTTP / scheduler entry point
in this module: every function takes an explicit caller-owned SQLite
connection.  ``apply_equipment_rebase`` is exercised against synthetic
databases in the isolated test suite; ``apply_approved_rebase`` is the
future authorised production entry that binds a REVIEWED proposal — it is
not reachable from any server code path and never accepts an external
clock.

Guarantees implemented here:

- every validation and write happens inside ONE transaction
  (``BEGIN IMMEDIATE``), committed only at the end — there is no window
  between the checks and the replace that an outside writer could slip
  into (the whole flow holds the reserved write lock);
- the current baseline fingerprint is re-read from the table AFTER the
  lock is taken and compared against the caller's expected fingerprint —
  a mismatch refuses the write (concurrent change protection);
- replacement is atomic: any failure rolls back and the old component
  table is left byte-identical;
- freshness is enforced against the REAL clock: ``apply_equipment_rebase``
  exposes ``now`` ONLY for tests; ``apply_approved_rebase`` has no time
  parameter at all — T is always the real wall clock.  The cutoff bucket B
  is read from the database as ``MAX(bucket_at)`` and must match the
  approved proposal; ``0 <= T - B <= 7200s`` must hold;
- the approved-proposal entry verifies, before any write: the caller's
  approved fingerprint against a stable fingerprint computed from the
  proposal payload (never a hash the file claims for itself), the live
  old-baseline fingerprint, B, and the recomputed candidate list with
  every per-member base price.  Any mismatch refuses and keeps the old
  table — no silent bucket/roster switch, no proposal auto-update;
- targeted rollback restores ONLY ``equipment_index_components`` and only
  when the current fingerprint equals the fingerprint produced by the
  applied rebase; a later legitimate change is never overwritten;
- quote snapshots, daily closes and every other table in the same
  database are never written (quotes are read only).

The candidate rules (ordering, select-then-validate, no refill, fallback
age gate) are the shared ones from ``equipment_rules.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .equipment_rules import EQUIPMENT_FALLBACK_MAX_AGE_SECONDS, EQUIPMENT_ORDER_BY

EQUIPMENT_COMPONENT_TARGET = 250
MIN_COMPONENTS = 200
MIN_COVERAGE_RATIO = 0.90
#: Freshness gate for a real execution: 0 <= T - B <= 7200 seconds.
EXECUTION_MAX_AGE_SECONDS = 7200


class EquipmentRebaseError(RuntimeError):
    """Base error for the internal rebase transaction functions."""


class FingerprintMismatch(EquipmentRebaseError):
    """The live baseline fingerprint differs from the expected one."""


class FreshnessViolation(EquipmentRebaseError):
    """The cutoff bucket B is not fresh enough against the execution time T."""


class ProposalMismatch(EquipmentRebaseError):
    """The proposal does not match the approved fingerprint or the live
    database state (B, roster or per-member prices)."""


@dataclass(frozen=True, slots=True)
class RebaseOutcome:
    applied_at: int
    bucket_b: int
    components: int
    covered: int
    fallback: int
    coverage: float
    fingerprint_before: str
    fingerprint_after: str


@dataclass(frozen=True, slots=True)
class ApprovedRebaseResult:
    """Everything an operator needs after an approved execution, including
    the material for a targeted rollback (the archived old rows and the
    post-apply fingerprint)."""

    applied_at: int
    bucket_b: int
    reference: int
    components: int
    covered: int
    fallback: int
    coverage: float
    fingerprint_before: str
    fingerprint_after: str
    archived_rows: list[tuple[str, str, float, int]]


def _market_day(timestamp: int) -> str:
    """Market-timezone (UTC+8) day of a timestamp — same rule as
    ``market_history._market_day`` (kept here to stay import-free)."""

    from datetime import datetime, timedelta, timezone

    return (datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=8)).strftime(
        "%Y-%m-%d"
    )


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def fingerprint_components(conn) -> str:
    """Stable fingerprint of the whole component table: sorted rows joined
    by the market name with every column serialised; empty table included."""

    rows = conn.execute(
        "SELECT market_name, base_day, base_price, added_at "
        "FROM equipment_index_components ORDER BY market_name COLLATE BINARY"
    ).fetchall()
    digest = hashlib.sha256()
    for market_name, base_day, base_price, added_at in rows:
        digest.update(
            f"{market_name}\x1f{base_day}\x1f{base_price}\x1f{added_at}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _read_components(conn) -> list[tuple[str, str, float, int]]:
    rows = conn.execute(
        "SELECT market_name, base_day, base_price, added_at "
        "FROM equipment_index_components ORDER BY market_name COLLATE BINARY"
    ).fetchall()
    return [
        (str(market_name), str(base_day), float(base_price), int(added_at))
        for market_name, base_day, base_price, added_at in rows
    ]


def _select_and_price(
    conn, *, since: int, bucket_b: int
) -> tuple[list[tuple[str, float]], int, int, int, list[str]]:
    """Shared rule selection: full deterministic sort (LIMIT target), then
    per-member pricing (cutoff hit, or in-age fallback).  Returns
    (entries, covered, fallback, selected_total, rejected).  Rejected
    members are never refilled."""

    rows = conn.execute(
        """
        SELECT market_name,
               COUNT(*) AS buckets,
               MAX(bucket_at) AS last_bucket
        FROM quote_snapshots
        WHERE bucket_at >= ? AND bucket_at <= ?
        GROUP BY market_name
        """
        + EQUIPMENT_ORDER_BY
        + """
        LIMIT ?
        """,
        (since, bucket_b, EQUIPMENT_COMPONENT_TARGET),
    ).fetchall()
    selected_total = len(rows)
    entries: list[tuple[str, float]] = []
    covered = 0
    fallback = 0
    rejected: list[str] = []
    for row_data in rows:
        name = str(row_data[0])
        price_row = conn.execute(
            "SELECT sell_price FROM quote_snapshots WHERE market_name = ? AND bucket_at = ?",
            (name, bucket_b),
        ).fetchone()
        price = _finite_positive(price_row[0] if price_row is not None else None)
        if price is not None:
            covered += 1
        else:
            source_row = conn.execute(
                "SELECT MAX(bucket_at) FROM quote_snapshots "
                "WHERE market_name = ? AND bucket_at <= ?",
                (name, bucket_b),
            ).fetchone()
            if source_row is None or source_row[0] is None:
                rejected.append(f"{name}:no price at-or-before B")
                continue
            source_bucket = int(source_row[0])
            age = bucket_b - source_bucket
            if age < 0 or age > EQUIPMENT_FALLBACK_MAX_AGE_SECONDS:
                rejected.append(f"{name}:fallback age {age}s exceeds 24h gate")
                continue
            price_row = conn.execute(
                "SELECT sell_price FROM quote_snapshots WHERE market_name = ? AND bucket_at = ?",
                (name, source_bucket),
            ).fetchone()
            price = _finite_positive(price_row[0] if price_row is not None else None)
            if price is None:
                rejected.append(f"{name}:fallback bucket missing or non-positive")
                continue
            fallback += 1
        entries.append((name, price))
    return entries, covered, fallback, selected_total, rejected


def _replace_components(
    conn, entries: Sequence[tuple[str, float]], *, base_day: str, applied_at: int
) -> None:
    """DELETE + INSERT the new component table inside the open transaction."""

    conn.execute("DELETE FROM equipment_index_components")
    conn.executemany(
        "INSERT INTO equipment_index_components "
        "(market_name, base_day, base_price, added_at) VALUES (?, ?, ?, ?)",
        [(name, base_day, price, applied_at) for name, price in entries],
    )


def apply_equipment_rebase(
    conn,
    *,
    now: int | None = None,
    expected_fingerprint: str | None = None,
) -> RebaseOutcome:
    """Atomically replace the component table with today's shared-rule list.

    ``now`` defaults to the real wall clock; it exists ONLY for tests.
    ``expected_fingerprint`` is the fingerprint the caller holds for the
    CURRENT baseline (e.g. taken from a rehearsal); the table is re-read
    inside the transaction and a mismatch refuses the whole replacement.
    """

    applied_at = int(now) if now is not None else int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        fingerprint_before = fingerprint_components(conn)
        if expected_fingerprint is not None and fingerprint_before != expected_fingerprint:
            raise FingerprintMismatch(
                "baseline fingerprint changed since it was read "
                f"({fingerprint_before[:12]}… != expected {expected_fingerprint[:12]}…)"
            )
        row = conn.execute("SELECT MAX(bucket_at) FROM quote_snapshots").fetchone()
        bucket_b = int(row[0]) if row and row[0] is not None else None
        if bucket_b is None:
            raise EquipmentRebaseError("no quote buckets available for a rebase")
        if bucket_b % 3600 != 0:
            raise EquipmentRebaseError(f"cutoff bucket B={bucket_b} is not hour-aligned")
        if bucket_b > applied_at:
            raise FreshnessViolation(
                f"cutoff bucket B={bucket_b} is after execution time T={applied_at} (future B)"
            )
        freshness = applied_at - bucket_b
        if freshness < 0 or freshness > EXECUTION_MAX_AGE_SECONDS:
            raise FreshnessViolation(
                f"execution freshness violated: T-B={freshness}s, "
                f"required 0 <= T-B <= {EXECUTION_MAX_AGE_SECONDS}s"
            )
        since = applied_at - 30 * 86400
        entries, covered, fallback, selected_total, rejected = _select_and_price(
            conn, since=since, bucket_b=bucket_b
        )
        if rejected:
            raise EquipmentRebaseError(
                f"{len(rejected)} selected member(s) rejected, whole rebase refused "
                f"(no refill): {'; '.join(rejected[:5])}"
            )
        components = len(entries)
        coverage = covered / selected_total if selected_total else 0.0
        if (
            selected_total < MIN_COMPONENTS
            or components < MIN_COMPONENTS
            or covered < MIN_COMPONENTS
            or coverage < MIN_COVERAGE_RATIO
        ):
            raise EquipmentRebaseError(
                f"gates not met: selected={selected_total} components={components} "
                f"covered={covered} coverage={coverage:.4f}"
            )
        base_day = _market_day(applied_at)
        _replace_components(conn, entries, base_day=base_day, applied_at=applied_at)
        fingerprint_after = fingerprint_components(conn)
        conn.commit()
        return RebaseOutcome(
            applied_at=applied_at,
            bucket_b=bucket_b,
            components=components,
            covered=covered,
            fallback=fallback,
            coverage=coverage,
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
        )
    except BaseException:
        conn.rollback()
        raise


def proposal_fingerprint(proposal: Mapping[str, Any]) -> str:
    """Stable fingerprint of an approved proposal payload.

    Computed from the payload itself (cutoff/reference + every member's
    name and base price, canonicalised) — the approved entry never trusts a
    hash that travels inside the proposal file.
    """

    cutoff = int(proposal["cutoff"])
    reference = int(proposal["reference"])
    members = sorted(
        proposal["components"],
        key=lambda m: str(m["market_name"]),
    )
    digest = hashlib.sha256()
    digest.update(f"cutoff={cutoff}\x1freference={reference}\n".encode("utf-8"))
    for member in members:
        digest.update(
            f"{member['market_name']}\x1f{json.dumps(member['base_price'])}\n".encode("utf-8")
        )
    return digest.hexdigest()


def apply_approved_rebase(
    conn,
    *,
    proposal: Mapping[str, Any],
    approved_fingerprint: str,
    expected_old_fingerprint: str,
) -> ApprovedRebaseResult:
    """Execute a REVIEWED, approved proposal atomically — the future
    authorised production entry.

    The real wall clock is the ONLY time source (no ``now`` parameter), so
    an arbitrary historical T cannot bypass the freshness check.

    Guarantee scope of the concurrency claim: ``BEGIN IMMEDIATE`` is issued
    as the very first statement of this function; from that point on the
    fingerprint re-read, every validation, the replace and the commit all
    run on the same connection inside that one reserved-write transaction.
    SQLite serialises writers, so no outside writer can interleave between
    the checks and the replace.  That is what the lock-conflict test
    demonstrates (a second writer holding the lock makes the entry fail
    with a lock error) — it does NOT prove anything about reads or about
    writers on OTHER databases; those are outside this guarantee.

    Contract, in order, all inside the same transaction and before any
    write:

    1. ``proposal_fingerprint(proposal)`` must equal the caller-approved
       fingerprint (the caller pins the reviewed bytes; a proposal whose
       content changed since review is refused even if a hash field inside
       it still matches);
    2. ``expected_old_fingerprint`` is REQUIRED (no default, missing or
       empty refuses) and is compared against the fingerprint re-read from
       the live table inside the transaction;
    3. B (proposal cutoff) must be hour-aligned and not after R (the
       proposal's own reference); R may be any second value — including
       R=B+1s or any non-hour-aligned instant — and the ranking window is
       ``R-30d .. B`` exactly as reviewed;
    4. B = ``MAX(bucket_at)`` must equal the proposal's cutoff — a newer
       bucket is never silently adopted;
    5. freshness 0 <= T-B <= 7200s with the real clock;
    6. the roster is recomputed with the shared rules and compared
       exactly with the proposal: both sides must be finite positive
       numbers and prices must be EXACTLY equal (no tolerance — a JSON
       round trip of a finite double is exact, so any real difference is
       refused).

    Any mismatch refuses the whole execution and keeps the old table; the
    proposal is never auto-updated.  On success the result carries the
    actual execution time, adopted parameters, both fingerprints and the
    archived old rows needed for a targeted rollback.
    """

    applied_at = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        if proposal_fingerprint(proposal) != approved_fingerprint:
            raise ProposalMismatch(
                "proposal content does not match the approved fingerprint "
                "(recompute refused; re-review the proposal before executing)"
            )
        if not expected_old_fingerprint:
            raise ProposalMismatch(
                "the caller must provide the reviewed old-baseline fingerprint "
                "(expected_old_fingerprint is required, never skipped)"
            )
        fingerprint_before = fingerprint_components(conn)
        if fingerprint_before != expected_old_fingerprint:
            raise FingerprintMismatch(
                "old baseline fingerprint differs from the reviewed one "
                f"({fingerprint_before[:12]}… != {expected_old_fingerprint[:12]}…)"
            )
        cutoff = int(proposal["cutoff"])
        reference = int(proposal["reference"])
        if cutoff > reference:
            raise ProposalMismatch(
                f"proposal cutoff B={cutoff} is after its reference R={reference} "
                "(B must not be after R; R itself may be any reviewed instant)"
            )
        row = conn.execute("SELECT MAX(bucket_at) FROM quote_snapshots").fetchone()
        live_b = int(row[0]) if row and row[0] is not None else None
        if live_b is None:
            raise EquipmentRebaseError("no quote buckets available for a rebase")
        if live_b != cutoff:
            raise ProposalMismatch(
                f"live latest bucket B={live_b} != approved proposal cutoff "
                f"B={cutoff} — refusing to silently adopt a newer bucket"
            )
        if cutoff % 3600 != 0:
            raise EquipmentRebaseError(f"cutoff bucket B={cutoff} is not hour-aligned")
        if cutoff > applied_at:
            raise FreshnessViolation(
                f"cutoff bucket B={cutoff} is after execution time T={applied_at} (future B)"
            )
        freshness = applied_at - cutoff
        if freshness < 0 or freshness > EXECUTION_MAX_AGE_SECONDS:
            raise FreshnessViolation(
                f"execution freshness violated: T-B={freshness}s, "
                f"required 0 <= T-B <= {EXECUTION_MAX_AGE_SECONDS}s"
            )
        since = reference - 30 * 86400
        entries, covered, fallback, selected_total, rejected = _select_and_price(
            conn, since=since, bucket_b=cutoff
        )
        if rejected:
            raise ProposalMismatch(
                f"{len(rejected)} member(s) no longer priceable under the reviewed "
                f"rules (no refill): {'; '.join(rejected[:5])}"
            )
        # Both sides must be finite positive numbers; then compare EXACTLY.
        proposal_by_name: dict[str, float] = {}
        for member in proposal["components"]:
            name = str(member["market_name"])
            price = float(member["base_price"])
            if not (math.isfinite(price) and price > 0):
                raise ProposalMismatch(
                    f"proposal member {name!r} has a non-finite or non-positive "
                    f"base price: {price!r}"
                )
            proposal_by_name[name] = price
        recomputed_by_name = dict(entries)
        for name, price in recomputed_by_name.items():
            if not (math.isfinite(price) and price > 0):
                raise ProposalMismatch(
                    f"recomputed member {name!r} is not finite positive: {price!r}"
                )
        if set(recomputed_by_name) != set(proposal_by_name):
            raise ProposalMismatch(
                "recomputed roster differs from the approved proposal roster "
                "(no refill, no silent roster switch)"
            )
        price_mismatches = [
            name for name in proposal_by_name if proposal_by_name[name] != recomputed_by_name[name]
        ]
        if price_mismatches:
            raise ProposalMismatch(
                "recomputed base prices differ from the approved proposal for: "
                + ", ".join(price_mismatches[:5])
            )
        components = len(entries)
        coverage = covered / selected_total if selected_total else 0.0
        if (
            selected_total < MIN_COMPONENTS
            or components < MIN_COMPONENTS
            or covered < MIN_COMPONENTS
            or coverage < MIN_COVERAGE_RATIO
        ):
            raise EquipmentRebaseError(
                f"gates not met: selected={selected_total} components={components} "
                f"covered={covered} coverage={coverage:.4f}"
            )
        archived_rows = _read_components(conn)
        base_day = _market_day(applied_at)
        _replace_components(conn, entries, base_day=base_day, applied_at=applied_at)
        fingerprint_after = fingerprint_components(conn)
        conn.commit()
        return ApprovedRebaseResult(
            applied_at=applied_at,
            bucket_b=cutoff,
            reference=reference,
            components=components,
            covered=covered,
            fallback=fallback,
            coverage=coverage,
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            archived_rows=archived_rows,
        )
    except BaseException:
        conn.rollback()
        raise


def rollback_equipment_components(
    conn,
    *,
    expected_current_fingerprint: str,
    archived_rows: Sequence[tuple[str, str, float, int]],
) -> int:
    """Restore ONLY the component table from ``archived_rows``.

    Refused (rollback, nothing written) when the live fingerprint is not
    the expected one — i.e. the table changed after the rebase, so the
    archived rows no longer represent the state to undo.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = fingerprint_components(conn)
        if current != expected_current_fingerprint:
            raise FingerprintMismatch(
                "current component fingerprint differs from the applied one; "
                "refusing targeted rollback so a later legitimate change is "
                f"not overwritten ({current[:12]}… != {expected_current_fingerprint[:12]}…)"
            )
        conn.execute("DELETE FROM equipment_index_components")
        conn.executemany(
            "INSERT INTO equipment_index_components "
            "(market_name, base_day, base_price, added_at) VALUES (?, ?, ?, ?)",
            archived_rows,
        )
        conn.commit()
        return len(archived_rows)
    except BaseException:
        conn.rollback()
        raise
