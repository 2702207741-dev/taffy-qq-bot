"""Daily LLM token accounting (schema v17 ``llm_usage_daily``).

One upsert per completed model call: provider label, model, request count
and token totals.  No content, no user ids — this table feeds the admin
panel's cost card and nothing else.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("taffy.services.llm_usage")


def _today_key() -> str:
    from datetime import datetime, timezone

    from .tz import to_market_tz

    return to_market_tz(datetime.now(timezone.utc)).strftime("%Y-%m-%d")


async def record_llm_usage(
    provider: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Best-effort upsert of one model call's usage; failures only log."""

    provider_clean = (provider or "unknown").strip()[:40]
    model_clean = (model or "unknown").strip()[:100]
    input_clean = max(0, int(input_tokens) if isinstance(input_tokens, (int, float)) else 0)
    output_clean = max(0, int(output_tokens) if isinstance(output_tokens, (int, float)) else 0)
    day = _today_key()
    try:
        from src.state.runtime import database

        await database.write(
            """
            INSERT INTO llm_usage_daily (day, provider, model, requests, input_tokens, output_tokens)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(day, provider, model) DO UPDATE SET
                requests = requests + 1,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens
            """,
            (day, provider_clean, model_clean, input_clean, output_clean),
        )
    except Exception as exc:
        logger.warning("LLM usage recording failed error=%s", type(exc).__name__)


__all__ = ["record_llm_usage"]
