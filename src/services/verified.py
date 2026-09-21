"""Typed trust boundary for externally sourced data.

Public renderers consume :class:`VerifiedResult` instead of loose dictionaries.
Provider names and response digests are intentionally internal metadata; market
reports must not expose them to QQ users.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class UnavailableReason(StrEnum):
    NOT_CONFIGURED = "not_configured"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    CIRCUIT_OPEN = "circuit_open"
    MALFORMED = "malformed"
    STALE = "stale"
    INSUFFICIENT_DATA = "insufficient_data"
    VALIDATION_FAILED = "validation_failed"


@dataclass(frozen=True, slots=True)
class DataProvenance:
    """Internal-only metadata proving where and when a value came from."""

    provider: str
    endpoint: str
    data_time: datetime | None
    fetched_at: datetime
    response_sha256: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.endpoint.strip():
            raise ValueError("provider and endpoint are required")
        if len(self.response_sha256) != 64:
            raise ValueError("response_sha256 must be a SHA-256 hex digest")
        int(self.response_sha256, 16)
        for value in (self.data_time, self.fetched_at):
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("provenance timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class VerifiedResult(Generic[T]):
    """A verified value or an explicit, machine-readable unavailable state."""

    value: T | None
    provenance: DataProvenance | None
    reason: UnavailableReason | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        available = self.value is not None
        if available != (self.provenance is not None) or available == (self.reason is not None):
            raise ValueError("verified result must be exactly available or unavailable")
        if self.detail is not None and len(self.detail) > 240:
            raise ValueError("unavailable detail is too long")

    @property
    def is_available(self) -> bool:
        return self.value is not None

    @classmethod
    def available(cls, value: T, provenance: DataProvenance) -> VerifiedResult[T]:
        return cls(value=value, provenance=provenance)

    @classmethod
    def unavailable(
        cls,
        reason: UnavailableReason,
        detail: str | None = None,
    ) -> VerifiedResult[T]:
        return cls(value=None, provenance=None, reason=reason, detail=detail)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def payload_sha256(payload: object) -> str:
    """Hash a canonical JSON representation without persisting the response."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def finite_number(value: object, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        return None
    return parsed


__all__ = [
    "DataProvenance",
    "UnavailableReason",
    "VerifiedResult",
    "finite_number",
    "payload_sha256",
    "utc_now",
]
