"""Shared framework for the SQLite state-layer repositories.

Every durable store composes these primitives instead of re-implementing
database wiring, argument validation, filter construction, pagination
clamping and total-count queries.  Validators are parameterised by field
name so the historical per-repository error messages stay byte-identical;
tests and callers rely on ``ValueError`` for every rejected argument.
"""

from __future__ import annotations

from src.core.database import StateDatabase

MAX_PAGE_SIZE = 1_000


class Repository:
    """Base class binding one durable store to the shared state database."""

    def __init__(self, database: StateDatabase) -> None:
        self.database = database

    async def initialize(self) -> None:
        await self.database.initialize()


def positive_int(value: object, *, name: str) -> int:
    """Reject booleans and non-positive integers for identifiers like user_id."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def nonnegative_int(value: object, *, name: str) -> int:
    """Reject booleans and negative integers for counters like group_id."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def text_value(value: object, *, name: str) -> str:
    """Require a plain string without imposing length or content limits."""

    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def page_bounds(limit: int, offset: int, *, maximum: int = MAX_PAGE_SIZE) -> tuple[int, int]:
    """Clamp a (limit, offset) page request into the shared safe window."""

    return max(1, min(int(limit), maximum)), max(0, int(offset))


class Filters:
    """Incremental equality builder producing one safe dynamic WHERE clause."""

    def __init__(self) -> None:
        self._conditions: list[str] = []
        self._parameters: list[object] = []

    def __bool__(self) -> bool:
        return bool(self._conditions)

    def equals(self, column: str, value: object) -> None:
        self._conditions.append(f"{column} = ?")
        self._parameters.append(value)

    @property
    def clause(self) -> str:
        """Render ``WHERE a = ? AND b = ?`` or an empty string."""

        return f"WHERE {' AND '.join(self._conditions)}" if self._conditions else ""

    @property
    def parameters(self) -> list[object]:
        return self._parameters


async def total_count(database: StateDatabase, table: str, filters: Filters) -> int:
    """Count rows of ``table`` honouring the given equality filters."""

    rows = await database.read(
        f"SELECT COUNT(*) AS total FROM {table} {filters.clause}",
        tuple(filters.parameters),
    )
    return int(rows[0]["total"]) if rows else 0
