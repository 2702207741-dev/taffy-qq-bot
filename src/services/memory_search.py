"""Hybrid memory retrieval: FTS5 trigram BM25 + optional vectors + RRF.

Two independent ranking routes feed a reciprocal-rank-fusion merge over the
caller's accepted insights (mem0-style multi-signal recall, single-file
SQLite edition):

* **BM25 route** — the ``learning_insights_fts`` trigram index (schema v18)
  ranked by ``bm25()``.  Trigram tokenization needs at least three visible
  characters, so shorter queries skip this route.
* **Vector route** — only when the semantic feature is enabled, the user is
  on the pilot allowlist and the embedding provider answers; brute-force
  cosine over stored 1024-dim float32 blobs.

Any route failure degrades honestly: the surviving route ranks alone, and
if neither answers (short query, empty index) the legacy term matcher keeps
the pre-V0.5 behaviour.  The returned channel label tells the caller — and
through it the user — exactly which path produced the answer.  Embeddings
are computed only over stored insight summaries (never raw chat text).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from ..state.learning_repository import LearningInsight
from ..state.persona_memory_repository import nonnegative_int, positive_int
from ..state.runtime import insight_embeddings, learning_insights
from .embedding import (
    cosine_similarity,
    decode_vector_blob,
    embed_single,
    embed_texts,
    embedding_model,
    encode_vector_blob,
    semantic_allowed_for_user,
    semantic_enabled,
)
from .learning import match_score, record_insight_hit

logger = logging.getLogger("taffy.services.memory_search")

_RRF_K = 60
_FTS_MIN_CHARS = 3
_FTS_MAX_PHRASE = 8
_FTS_MAX_TERMS = 12
_MAX_SCOPE_ROWS = 500
_MAX_HITS = 8
_BACKFILL_BATCH = 32
_MIN_COSINE = 0.30

Channel = Literal["hybrid", "bm25", "vector", "legacy"]

_CHANNEL_LABELS: dict[str, str] = {
    "hybrid": "混合（关键词+语义）",
    "bm25": "关键词",
    "vector": "语义",
    "legacy": "关键词（简短词直配）",
}


@dataclass(frozen=True, slots=True)
class HybridHit:
    insight: LearningInsight
    score: float


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    hits: tuple[HybridHit, ...]
    channel: Channel
    note: str | None = None

    @property
    def channel_label(self) -> str:
        return _CHANNEL_LABELS.get(self.channel, self.channel)


def _fts_phrase(query: str) -> str:
    """Quote one query segment as a single FTS5 phrase term (value, not SQL)."""

    quote = '"'
    escaped = query.replace(quote, quote * 2)
    return quote + escaped + quote


def _fts_terms(query: str) -> tuple[str, ...] | None:
    """Build the OR-joined FTS5 term list for one free-form query.

    Whitespace/punctuation segments each become one quoted phrase; a long
    CJK segment (no natural word boundaries) is widened into overlapping
    trigram windows so a partial keyword still recalls its memory.  Only
    structural ``OR`` keywords are introduced here; every term body stays a
    quoted value, so no user text can change the query grammar.  ``None``
    means no segment is long enough for trigram matching.
    """

    import re

    segments = [segment for segment in re.split(r"[\s，。？！、；：,.?!;:]+", query) if segment]
    terms: list[str] = []
    for segment in segments:
        if len(segment) < _FTS_MIN_CHARS:
            continue
        if len(segment) <= _FTS_MAX_PHRASE:
            terms.append(_fts_phrase(segment))
            continue
        windows = [
            segment[start : start + _FTS_MIN_CHARS]
            for start in range(len(segment) - _FTS_MIN_CHARS + 1)
        ]
        for window in windows:
            if len(terms) >= _FTS_MAX_TERMS:
                break
            terms.append(_fts_phrase(window))
        if len(terms) >= _FTS_MAX_TERMS:
            break
    return tuple(terms) or None


async def _bm25_ranked(
    user_id: int, scope_group_id: int, query: str, *, kind: str | None
) -> tuple[tuple[int, ...] | None, str | None]:
    """Rank insight ids with FTS5 BM25; (None, reason) when route unavailable."""

    terms = _fts_terms(query)
    if terms is None:
        return None, "查询词太短，跳过关键词索引"
    match_expr = " OR ".join(terms)
    kind_clause = " AND i.kind = ?" if kind is not None else ""
    parameters: list[object] = [match_expr, user_id, scope_group_id]
    if kind is not None:
        parameters.append(kind)
    sql = f"""
        SELECT i.id FROM learning_insights_fts f
        JOIN learning_insights i ON i.id = f.rowid
        WHERE learning_insights_fts MATCH ?
          AND i.user_id = ? AND i.scope_group_id = ?
          AND i.status = 'accepted'{kind_clause}
        ORDER BY bm25(learning_insights_fts), i.weight DESC, i.id
        LIMIT ?
    """
    from src.state.runtime import database

    try:
        rows = await database.read(sql, (*parameters, _MAX_SCOPE_ROWS))
    except Exception as exc:
        logger.warning("FTS5 route failed error=%s", type(exc).__name__)
        return None, "关键词索引暂不可用"
    return tuple(int(row["id"]) for row in rows), None


async def _scope_insights(
    user_id: int, scope_group_id: int, *, kind: str | None
) -> tuple[LearningInsight, ...]:
    return await learning_insights.list_scope(user_id, scope_group_id, status="accepted", kind=kind)


async def _ensure_embeddings(items: tuple[LearningInsight, ...], user_id: int) -> bool:
    """Backfill missing/stale embeddings for this scope; False when unable.

    Only stored insight summaries cross the network boundary — the same
    reviewable text the knowledge command already displays.
    """

    if not semantic_allowed_for_user(user_id) or not items:
        return False
    model = embedding_model()
    current = int(time.time())
    wanted: dict[int, str] = {}
    for item in items[:_MAX_SCOPE_ROWS]:
        wanted[item.id] = item.content
    try:
        stored = await insight_embeddings.get(list(wanted), model=model)
    except Exception as exc:
        logger.warning("Embedding load failed error=%s", type(exc).__name__)
        return False
    missing = [
        (insight_id, content) for insight_id, content in wanted.items() if insight_id not in stored
    ]
    stale = [
        (item.id, item.content)
        for item in items
        if item.id in stored and stored[item.id].created_at < item.updated_at
    ]
    pending = (missing + stale)[:_BACKFILL_BATCH]
    if not pending:
        return True
    vectors = await embed_texts([content for _id, content in pending])
    if vectors is None:
        return False
    wrote_all = True
    for (insight_id, _content), vector in zip(pending, vectors, strict=True):
        blob = encode_vector_blob(vector)
        if blob is None:
            wrote_all = False
            continue
        try:
            await insight_embeddings.put(insight_id, embedding=blob, model=model, now=current)
        except Exception as exc:
            logger.warning("Embedding store failed error=%s", type(exc).__name__)
            wrote_all = False
    return wrote_all


async def _vector_ranked(
    items: tuple[LearningInsight, ...], query: str, user_id: int
) -> tuple[tuple[int, ...] | None, str | None]:
    """Rank insight ids by cosine similarity; (None, reason) when unavailable."""

    if not semantic_enabled():
        return None, None
    if not semantic_allowed_for_user(user_id):
        return None, None
    try:
        ready = await _ensure_embeddings(items, user_id)
    except Exception as exc:
        logger.warning("Embedding backfill failed error=%s", type(exc).__name__)
        return None, "语义通道暂时不可用"
    if not ready and not items:
        return None, None
    model = embedding_model()
    try:
        query_vector = await embed_single(query)
    except Exception as exc:
        logger.warning("Query embedding failed error=%s", type(exc).__name__)
        return None, "语义通道暂时不可用"
    if query_vector is None:
        return None, "语义通道暂时不可用"
    try:
        stored = await insight_embeddings.get([item.id for item in items], model=model)
    except Exception as exc:
        logger.warning("Embedding reload failed error=%s", type(exc).__name__)
        return None, "语义通道暂时不可用"
    scored: list[tuple[float, int]] = []
    for item in items:
        row = stored.get(item.id)
        if row is None:
            continue
        vector = decode_vector_blob(row.embedding)
        if vector is None:
            continue
        similarity = cosine_similarity(query_vector, vector)
        if similarity >= _MIN_COSINE:
            scored.append((similarity, item.id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return tuple(insight_id for _similarity, insight_id in scored), None


def _rrf_fuse(route_a: tuple[int, ...] | None, route_b: tuple[int, ...] | None) -> dict[int, float]:
    """Reciprocal-rank fusion over the available route rankings."""

    fused: dict[int, float] = {}
    for ranking in (route_a, route_b):
        if not ranking:
            continue
        for position, insight_id in enumerate(ranking):
            fused[insight_id] = fused.get(insight_id, 0.0) + 1.0 / (_RRF_K + position + 1)
    return fused


async def hybrid_search(
    user_id: int,
    scope_group_id: int,
    query: str,
    *,
    kind: str | None = None,
    limit: int = _MAX_HITS,
) -> HybridSearchResult:
    """Search one scope's accepted insights across both routes with RRF.

    The channel label reports what actually ran so replies can stay honest
    about degradation; every failure path still returns usable (legacy)
    results when they exist.
    """

    owner = positive_int(user_id, name="user_id")
    scope = nonnegative_int(scope_group_id, name="scope_group_id")
    clean_query = str(query).strip()[:100]
    if not clean_query:
        return HybridSearchResult((), "legacy", "查询为空")
    bounded_limit = max(1, min(int(limit), _MAX_HITS))

    items = await _scope_insights(owner, scope, kind=kind)
    if not items:
        return HybridSearchResult((), "legacy")

    by_id = {item.id: item for item in items}
    bm25_ranking, bm25_note = await _bm25_ranked(owner, scope, clean_query, kind=kind)
    vector_ranking, vector_note = await _vector_ranked(items, clean_query, owner)

    fused = _rrf_fuse(bm25_ranking, vector_ranking)
    if fused:
        ordered = sorted(
            fused.items(), key=lambda pair: (-pair[1], -by_id[pair[0]].weight, pair[0])
        )
        hits = tuple(HybridHit(by_id[insight_id], score) for insight_id, score in ordered)
        if bm25_ranking is not None and vector_ranking is not None:
            channel: Channel = "hybrid"
        elif vector_ranking is not None:
            channel = "vector"
        else:
            channel = "bm25"
        notes = [note for note in (bm25_note, vector_note) if note]
        return HybridSearchResult(
            hits[:bounded_limit], channel, "；".join(notes) if notes else None
        )

    # Both semantic routes produced nothing: keep the legacy term matcher so
    # short queries and index gaps behave exactly like the pre-V0.5 path.
    legacy = sorted(
        ((match_score(clean_query, item.content), item) for item in items),
        key=lambda pair: (pair[0], pair[1].weight, pair[1].confidence),
        reverse=True,
    )
    hits = tuple(HybridHit(item, float(score)) for score, item in legacy if score > 0)
    note = bm25_note or vector_note
    return HybridSearchResult(hits[:bounded_limit], "legacy", note)


async def hybrid_search_with_reinforcement(
    user_id: int,
    scope_group_id: int,
    query: str,
    *,
    kind: str | None = None,
    limit: int = _MAX_HITS,
) -> HybridSearchResult:
    """hybrid_search plus access-reinforcement for the top knowledge hit."""

    result = await hybrid_search(user_id, scope_group_id, query, kind=kind, limit=limit)
    for hit in result.hits:
        if hit.insight.kind == "knowledge":
            await record_insight_hit(hit.insight.id)
            break
    return result


__all__ = [
    "Channel",
    "HybridHit",
    "HybridSearchResult",
    "hybrid_search",
    "hybrid_search_with_reinforcement",
]
