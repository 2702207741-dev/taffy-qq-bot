"""Optional semantic embeddings for accepted learning insights.

Only already-sanitized distilled facts (accepted insight summaries, which
never contain raw conversation text) are ever sent to the embedding
provider — raw chat text is never vectorized, keeping the learning-privacy
red line intact.  Vectors are 1024-dim float32 (bge-m3) stored as
4096-byte BLOBs in the ``insight_embeddings`` table and scored with
brute-force cosine, which is comfortably fast at memory scale (thousands
of rows).

Everything is fail-closed: an unconfigured key, a disabled switch, a
non-pilot user, an exhausted daily budget or any transport/parse error
simply means "no vector", and the hybrid retriever degrades to the lexical
route without guessing.
"""

from __future__ import annotations

import logging
import math
import struct

from src.core.database import Transaction

from .config import (
    bounded_base_url,
    bounded_env_int,
    bounded_env_text,
    env_bool,
    env_secret,
)
from .http_client import request_json
from .tz import to_market_tz

logger = logging.getLogger("taffy.services.embedding")

_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
_DEFAULT_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024
_BLOB_BYTES = EMBEDDING_DIM * 4
_TIMEOUT_SECONDS = bounded_env_int("TAFFY_EMBEDDING_TIMEOUT_SECONDS", 15, 5, 60)
_DAILY_BUDGET = bounded_env_int("TAFFY_EMBEDDING_DAILY_BUDGET", 200, 0, 10_000)
_MAX_BATCH = 32
_MAX_INPUT_CHARS = 512

_EMBEDDING_BASE_URL = bounded_base_url("TAFFY_EMBEDDING_BASE_URL", _DEFAULT_BASE_URL)
_EMBEDDING_MODEL = bounded_env_text("TAFFY_EMBEDDING_MODEL", _DEFAULT_MODEL, maximum=100)

_embedding_base_url = _EMBEDDING_BASE_URL
_embedding_model = _EMBEDDING_MODEL


def semantic_enabled() -> bool:
    """Feature switch plus endpoint/key readiness (user pilot gate is separate)."""

    if not env_bool("TAFFY_MEMORY_SEMANTIC_ENABLED", False):
        return False
    if not _embedding_base_url or not _embedding_model:
        return False
    return bool(embedding_api_key())


def embedding_api_key() -> str:
    return env_secret("TAFFY_EMBEDDING_API_KEY")


def embedding_model() -> str:
    return _embedding_model


def semantic_allowed_for_user(user_id: object) -> bool:
    """Pilot discipline for outbound embedding calls (fallback: SUPERUSERS)."""

    import re

    if not semantic_enabled():
        return False
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        return False
    try:
        owner = int(user_id)
    except ValueError:
        return False
    if owner <= 0:
        return False

    from .config import runtime_setting

    explicit = runtime_setting("TAFFY_MEMORY_SEMANTIC_PILOT_USERS") or ""
    values = re.findall(r"\d+", explicit)
    if not values:
        values = re.findall(r"\d+", runtime_setting("SUPERUSERS") or "")
    return owner in {int(value) for value in values if int(value) > 0}


def _today_key() -> str:
    from datetime import datetime, timezone

    return to_market_tz(datetime.now(timezone.utc)).strftime("%Y-%m-%d")


async def _consume_daily_budget() -> bool:
    """Reserve one embedding API call for the current market day.

    Read, compare and increment run in a single transaction: as separate calls
    each one gets its own connection, so concurrent requests all observed the
    same `used` and every one of them passed the check, spending past the daily
    cap on a paid endpoint.
    """

    from src.state.runtime import database

    key = f"embedding:{_today_key()}"

    def reserve(transaction: Transaction) -> bool:
        rows = transaction.read("SELECT value FROM app_meta WHERE key = ?", (key,))
        used = int(rows[0]["value"]) if rows else 0
        if used < 0 or used >= _DAILY_BUDGET:
            return False
        transaction.write(
            """
            INSERT INTO app_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + 1, updated_at =
                CAST(strftime('%s', 'now') AS INTEGER)
            """,
            (key, str(used + 1)),
        )
        return True

    try:
        return await database.transaction(reserve)
    except Exception as exc:
        logger.warning("Embedding budget check failed error=%s", type(exc).__name__)
        return False


def parse_embeddings_response(payload: object) -> list[list[float]] | None:
    """Validate one ``/embeddings`` payload; None on any structural mismatch."""

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    vectors: list[list[float]] = []
    for entry in data:
        if not isinstance(entry, dict):
            return None
        vector = entry.get("embedding")
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIM:
            return None
        floats: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number = float(value)
            if not math.isfinite(number):
                return None
            floats.append(number)
        vectors.append(floats)
    return vectors


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed one bounded batch of sanitized insight summaries; None on failure.

    Callers guarantee the inputs are stored insight contents (already
    summarized, reviewable, deletable) — never raw conversation text.
    """

    if not semantic_enabled() or not texts:
        return None
    batch = [str(text)[:_MAX_INPUT_CHARS] for text in texts[:_MAX_BATCH]]
    if not await _consume_daily_budget():
        logger.info("Embedding daily budget exhausted; skipping batch")
        return None
    try:
        response = await request_json(
            "POST",
            f"{_embedding_base_url}/embeddings",
            attempts=1,
            timeout=_TIMEOUT_SECONDS,
            total_timeout=min(float(_TIMEOUT_SECONDS + 5), 60.0),
            headers={
                "Authorization": f"Bearer {embedding_api_key()}",
                "Content-Type": "application/json",
            },
            json={"model": _embedding_model, "input": batch, "encoding_format": "float"},
        )
    except Exception as exc:
        logger.warning("Embedding request failed error=%s", type(exc).__name__)
        return None
    parsed = parse_embeddings_response(response)
    if parsed is None or len(parsed) != len(batch):
        logger.info("Embedding response rejected by validation")
        return None
    return parsed


def encode_vector_blob(vector: list[float]) -> bytes | None:
    """Pack one validated vector into the canonical float32 BLOB."""

    if len(vector) != EMBEDDING_DIM:
        return None
    return struct.pack(f"<{EMBEDDING_DIM}f", *(float(value) for value in vector))


def decode_vector_blob(blob: object) -> list[float] | None:
    """Unpack one stored BLOB; None on any size or type mismatch."""

    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    payload = bytes(blob)
    if len(payload) != _BLOB_BYTES:
        return None
    return list(struct.unpack(f"<{EMBEDDING_DIM}f", payload))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Brute-force cosine over two equal-length vectors (0 for degenerate)."""

    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


async def embed_single(text: str) -> list[float] | None:
    """Embed one query string (never persisted, never raw chat text)."""

    vectors = await embed_texts([text])
    if not vectors:
        return None
    return vectors[0]


def reset_runtime_state() -> None:
    """Test seam; the embedding client is stateless by design."""


__all__ = [
    "EMBEDDING_DIM",
    "cosine_similarity",
    "decode_vector_blob",
    "embed_single",
    "embed_texts",
    "embedding_api_key",
    "embedding_model",
    "encode_vector_blob",
    "parse_embeddings_response",
    "semantic_allowed_for_user",
    "semantic_enabled",
]
