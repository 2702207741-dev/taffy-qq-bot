"""Persisted insight vectors for the optional semantic retrieval channel.

The repository keeps one float32 BLOB per insight (see
``insight_embeddings`` in schema v18).  Rows are written lazily by the
hybrid retriever's backfill step and are keyed by insight id; a stale row
(one older than the insight's ``updated_at``) is simply replaced the next
time that insight participates in a semantic search.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.core.database import StateDatabase

from .repository import positive_int

_EMBEDDING_BYTES = 4096


@dataclass(frozen=True, slots=True)
class StoredEmbedding:
    insight_id: int
    embedding: bytes
    model: str
    created_at: int


def _row_to_embedding(row: dict[str, Any]) -> StoredEmbedding:
    blob = row["embedding"]
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("stored embedding blob is invalid")
    payload = bytes(blob)
    if len(payload) != _EMBEDDING_BYTES:
        raise ValueError("stored embedding blob has an unexpected size")
    return StoredEmbedding(
        insight_id=int(row["insight_id"]),
        embedding=payload,
        model=str(row["model"]),
        created_at=int(row["created_at"]),
    )


class InsightEmbeddingRepository:
    """BLOB storage backing the brute-force cosine route."""

    def __init__(self, database: StateDatabase) -> None:
        self.database = database

    async def get(self, insight_ids: list[int], *, model: str) -> dict[int, StoredEmbedding]:
        """Return current-model embeddings for the given insight ids."""

        if not insight_ids:
            return {}
        bounded = [positive_int(value, name="insight_id") for value in insight_ids[:500]]
        placeholders = ", ".join("?" for _ in bounded)
        rows = await self.database.read(
            f"""
            SELECT insight_id, embedding, model, created_at FROM insight_embeddings
            WHERE model = ? AND insight_id IN ({placeholders})
            """,
            (model, *bounded),
        )
        found: dict[int, StoredEmbedding] = {}
        for row in rows:
            try:
                stored = _row_to_embedding(row)
            except ValueError:
                continue
            found[stored.insight_id] = stored
        return found

    async def put(
        self,
        insight_id: int,
        *,
        embedding: bytes,
        model: str,
        now: int | None = None,
    ) -> bool:
        """Insert or refresh one embedding; False on validation failure."""

        positive_int(insight_id, name="insight_id")
        if not isinstance(embedding, (bytes, bytearray, memoryview)):
            return False
        payload = bytes(embedding)
        if len(payload) != _EMBEDDING_BYTES or not model.strip():
            return False
        timestamp = int(time.time() if now is None else now)
        await self.database.write(
            """
            INSERT INTO insight_embeddings (insight_id, embedding, model, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(insight_id) DO UPDATE SET
                embedding = excluded.embedding,
                model = excluded.model,
                created_at = excluded.created_at
            """,
            (insight_id, payload, model.strip()[:100], timestamp),
        )
        return True

    async def drop_other_models(self, *, model: str) -> int:
        """Remove rows written by a previous embedding model (bounded sweep)."""

        result = await self.database.write(
            """
            DELETE FROM insight_embeddings
            WHERE model != ? AND insight_id IN (
                SELECT insight_id FROM insight_embeddings WHERE model != ? LIMIT 500
            )
            """,
            (model, model),
        )
        return max(0, result.rowcount)


__all__ = ["InsightEmbeddingRepository", "StoredEmbedding"]
