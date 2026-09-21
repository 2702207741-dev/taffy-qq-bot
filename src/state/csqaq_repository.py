"""Durable market_hash_name → CSQAQ good_id mapping cache.

CSQAQ allows one request per second per IP, so every resolved mapping is
worth persisting: the adapter consults this table before any network call
and stores each newly resolved id.  Stale rows are never served past the
caller-provided maximum age; they simply miss and get re-resolved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.core.database import StateDatabase, Transaction
from src.state.repository import Repository

_MAX_NAME_LENGTH = 256


@dataclass(frozen=True, slots=True)
class CachedGoodId:
    """One resolved CSQAQ mapping as stored in the cache table."""

    good_id: int
    csqaq_name: str
    resolved_at: int


def _validated_name(market_hash_name: str) -> str:
    normalized = market_hash_name.strip()
    if not normalized or len(normalized) > _MAX_NAME_LENGTH:
        raise ValueError("market_hash_name must be 1..256 characters")
    return normalized


class CsqaqGoodIdRepository(Repository):
    """Read-through cache for CSQAQ good ids keyed by market_hash_name."""

    def __init__(self, database: StateDatabase) -> None:
        super().__init__(database)

    async def get(self, market_hash_name: str, *, max_age_seconds: int) -> CachedGoodId | None:
        """Return the cached mapping only while it is younger than the age cap."""

        name = _validated_name(market_hash_name)
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
            raise ValueError("max_age_seconds must be an integer")
        if max_age_seconds <= 0:
            return None

        def lookup(transaction: Transaction) -> CachedGoodId | None:
            rows = transaction.read(
                """
                SELECT good_id, csqaq_name, resolved_at
                FROM csqaq_good_id_cache
                WHERE market_hash_name = ?
                """,
                (name,),
            )
            if not rows:
                return None
            row = rows[0]
            resolved_at = int(row["resolved_at"])
            return CachedGoodId(
                good_id=int(row["good_id"]),
                csqaq_name=str(row["csqaq_name"]),
                resolved_at=resolved_at,
            )

        cached = await self.database.transaction(lookup)
        if cached is None:
            return None
        if time.time() - cached.resolved_at > max_age_seconds:
            return None
        return cached

    async def put(
        self,
        market_hash_name: str,
        *,
        good_id: int,
        csqaq_name: str,
        resolved_at: int | None = None,
    ) -> CachedGoodId:
        """Insert or refresh one mapping; later resolutions win."""

        name = _validated_name(market_hash_name)
        if isinstance(good_id, bool) or not isinstance(good_id, int) or good_id <= 0:
            raise ValueError("good_id must be a positive integer")
        resolved_name = csqaq_name.strip()
        if not resolved_name or len(resolved_name) > 200:
            raise ValueError("csqaq_name must be 1..200 characters")

        def persist(transaction: Transaction) -> CachedGoodId:
            stamp = int(resolved_at if resolved_at is not None else time.time())
            if stamp < 0:
                raise ValueError("resolved_at must not be negative")
            transaction.write(
                """
                INSERT INTO csqaq_good_id_cache (
                    market_hash_name, good_id, csqaq_name, resolved_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (market_hash_name) DO UPDATE SET
                    good_id = excluded.good_id,
                    csqaq_name = excluded.csqaq_name,
                    resolved_at = excluded.resolved_at
                """,
                (name, good_id, resolved_name, stamp),
            )
            return CachedGoodId(good_id=good_id, csqaq_name=resolved_name, resolved_at=stamp)

        return await self.database.transaction(persist)

    async def purge_older_than(self, *, cutoff: int) -> int:
        """Delete mappings last resolved before the cutoff; returns rowcount."""

        if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff < 0:
            raise ValueError("cutoff must be a non-negative integer")

        def cleanup(transaction: Transaction) -> int:
            result = transaction.write(
                "DELETE FROM csqaq_good_id_cache WHERE resolved_at < ?",
                (cutoff,),
            )
            return result.rowcount

        return await self.database.transaction(cleanup)
