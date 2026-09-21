"""Opt-in, scope-isolated persona style learning without raw feedback storage."""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass

from src.core.database import Transaction
from src.services.persona import PersonaStyleContext

from .persona_memory_repository import PersonaScope
from .repository import Repository

MAX_FEEDBACK_PER_SCOPE = 50
FEEDBACK_RETENTION_SECONDS = 90 * 24 * 60 * 60

_STYLE_FIELDS = ("concise", "warm", "teasing", "proactive", "catchphrase", "direct")


@dataclass(frozen=True, slots=True)
class PersonaStyleDelta:
    concise: int = 0
    warm: int = 0
    teasing: int = 0
    proactive: int = 0
    catchphrase: int = 0
    direct: int = 0
    positive: bool = False

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not -2 <= value <= 2
            for value in self.values
        ):
            raise ValueError("style deltas must be integers in [-2, 2]")

    @property
    def values(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.concise,
            self.warm,
            self.teasing,
            self.proactive,
            self.catchphrase,
            self.direct,
        )

    @property
    def has_adjustment(self) -> bool:
        return self.positive or any(self.values)


def _style_values(style: PersonaStyleContext) -> tuple[int, int, int, int, int, int]:
    return (
        style.concise,
        style.warm,
        style.teasing,
        style.proactive,
        style.catchphrase,
        style.direct,
    )


@dataclass(frozen=True, slots=True)
class PersonaStyleProfile:
    scope: PersonaScope
    learning_enabled: bool = False
    style: PersonaStyleContext = PersonaStyleContext()
    updated_at: int = 0


def parse_style_feedback(message: str) -> PersonaStyleDelta | None:
    """Map only explicit, local phrases to bounded parameters."""

    text = unicodedata.normalize("NFKC", str(message)).strip().casefold()
    if not text or len(text) > 80:
        return None
    values = {
        name: 0 for name in ("concise", "warm", "teasing", "proactive", "catchphrase", "direct")
    }
    positive = any(marker in text for marker in ("这句很像塔菲", "这句像塔菲", "这次很像塔菲"))
    rules: tuple[tuple[tuple[str, ...], str, int], ...] = (
        (("太客服", "别像客服", "客服腔"), "direct", 1),
        (("太长", "短一点", "简短一点", "再短点"), "concise", 1),
        (("详细一点", "多解释一点"), "concise", -1),
        (("少点喵", "别老喵", "别加喵"), "catchphrase", -1),
        (("多点喵",), "catchphrase", 1),
        (("损一点", "再损点", "调侃一点"), "teasing", 1),
        (("别太损", "少损我", "别调侃"), "teasing", -1),
        (("温柔一点", "暖一点", "多安慰"), "warm", 1),
        (("别太温柔", "少安慰"), "warm", -1),
        (("主动一点", "多给建议"), "proactive", 1),
        (("别问那么多", "少追问", "别延伸"), "proactive", -1),
        (("直接一点", "别绕", "先说结论"), "direct", 1),
        (("委婉一点", "别太直接"), "direct", -1),
    )
    for markers, field, delta in rules:
        if any(marker in text for marker in markers):
            values[field] = max(-2, min(2, values[field] + delta))
    result = PersonaStyleDelta(**values, positive=positive)
    return result if result.has_adjustment else None


def is_ambiguous_style_feedback(message: str) -> bool:
    text = unicodedata.normalize("NFKC", str(message)).strip().casefold()
    return any(
        marker in text
        for marker in ("不像塔菲", "不够塔菲", "更像塔菲一点", "感觉不对", "这句不行")
    )


def _clip(value: int) -> int:
    return max(-2, min(2, value))


def _stored_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("stored persona style integer is invalid")
    return value


def _profile_from_row(scope: PersonaScope, row: dict[str, object] | None) -> PersonaStyleProfile:
    if row is None:
        return PersonaStyleProfile(scope)
    return PersonaStyleProfile(
        scope=scope,
        learning_enabled=bool(_stored_int(row["learning_enabled"])),
        style=PersonaStyleContext(
            concise=_stored_int(row["concise"]),
            warm=_stored_int(row["warm"]),
            teasing=_stored_int(row["teasing"]),
            proactive=_stored_int(row["proactive"]),
            catchphrase=_stored_int(row["catchphrase"]),
            direct=_stored_int(row["direct"]),
        ),
        updated_at=_stored_int(row["updated_at"]),
    )


class PersonaStyleRepository(Repository):
    async def get(self, scope: PersonaScope) -> PersonaStyleProfile:
        rows = await self.database.read(
            """SELECT * FROM persona_style_profiles
               WHERE owner_user_id = ? AND scope_group_id = ?""",
            (scope.owner_user_id, scope.group_id),
        )
        return _profile_from_row(scope, rows[0] if rows else None)

    async def set_learning(self, scope: PersonaScope, enabled: bool) -> PersonaStyleProfile:
        now = int(time.time())
        await self.database.write(
            """
            INSERT INTO persona_style_profiles
                (owner_user_id, scope_group_id, learning_enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (owner_user_id, scope_group_id) DO UPDATE SET
                learning_enabled = excluded.learning_enabled,
                updated_at = excluded.updated_at
            """,
            (scope.owner_user_id, scope.group_id, int(bool(enabled)), now),
        )
        return await self.get(scope)

    async def apply_feedback(
        self,
        scope: PersonaScope,
        reply_sha256: str,
        scene: str,
        delta: PersonaStyleDelta,
        *,
        now: int | None = None,
    ) -> PersonaStyleProfile:
        if len(reply_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in reply_sha256):
            raise ValueError("reply_sha256 must be lowercase SHA-256")
        clean_scene = str(scene).strip()[:40]
        if not clean_scene:
            raise ValueError("scene is required")
        timestamp = int(time.time()) if now is None else max(0, int(now))

        def apply(transaction: Transaction) -> PersonaStyleProfile:
            rows = transaction.read(
                """SELECT * FROM persona_style_profiles
                   WHERE owner_user_id = ? AND scope_group_id = ?""",
                (scope.owner_user_id, scope.group_id),
            )
            current = _profile_from_row(scope, rows[0] if rows else None)
            if not current.learning_enabled:
                raise ValueError("persona learning is disabled")
            duplicate = transaction.read(
                """SELECT 1 AS present FROM persona_feedback_events
                   WHERE owner_user_id=? AND scope_group_id=? AND reply_sha256=? LIMIT 1""",
                (scope.owner_user_id, scope.group_id, reply_sha256),
            )
            if duplicate:
                raise ValueError("feedback for this reply was already recorded")
            old_values = current.style
            new_values = tuple(
                _clip(value + change)
                for value, change in zip(_style_values(old_values), delta.values, strict=True)
            )
            actual = tuple(
                new - old for new, old in zip(new_values, _style_values(old_values), strict=True)
            )
            transaction.write(
                """
                INSERT INTO persona_style_profiles (
                    owner_user_id, scope_group_id, learning_enabled, concise, warm,
                    teasing, proactive, catchphrase, direct, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (owner_user_id, scope_group_id) DO UPDATE SET
                    concise=excluded.concise, warm=excluded.warm, teasing=excluded.teasing,
                    proactive=excluded.proactive, catchphrase=excluded.catchphrase,
                    direct=excluded.direct, updated_at=excluded.updated_at
                """,
                (scope.owner_user_id, scope.group_id, *new_values, timestamp),
            )
            transaction.write(
                """INSERT INTO persona_feedback_events (
                    owner_user_id, scope_group_id, reply_sha256, scene,
                    concise_delta, warm_delta, teasing_delta, proactive_delta,
                    catchphrase_delta, direct_delta, positive, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scope.owner_user_id,
                    scope.group_id,
                    reply_sha256,
                    clean_scene,
                    *actual,
                    int(delta.positive),
                    timestamp,
                ),
            )
            transaction.write(
                """DELETE FROM persona_feedback_events
                   WHERE owner_user_id = ? AND scope_group_id = ? AND created_at < ?""",
                (scope.owner_user_id, scope.group_id, timestamp - FEEDBACK_RETENTION_SECONDS),
            )
            transaction.write(
                """DELETE FROM persona_feedback_events WHERE id IN (
                    SELECT id FROM persona_feedback_events
                    WHERE owner_user_id = ? AND scope_group_id = ?
                    ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?
                )""",
                (scope.owner_user_id, scope.group_id, MAX_FEEDBACK_PER_SCOPE),
            )
            return PersonaStyleProfile(scope, True, PersonaStyleContext(*new_values), timestamp)

        return await self.database.transaction(apply)

    async def undo(self, scope: PersonaScope) -> PersonaStyleProfile | None:
        timestamp = int(time.time())

        def undo_last(transaction: Transaction) -> PersonaStyleProfile | None:
            rows = transaction.read(
                """SELECT * FROM persona_feedback_events
                   WHERE owner_user_id = ? AND scope_group_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (scope.owner_user_id, scope.group_id),
            )
            if not rows:
                return None
            event = rows[0]
            profile_rows = transaction.read(
                """SELECT * FROM persona_style_profiles
                   WHERE owner_user_id = ? AND scope_group_id = ?""",
                (scope.owner_user_id, scope.group_id),
            )
            profile = _profile_from_row(scope, profile_rows[0] if profile_rows else None)
            old = profile.style
            deltas = tuple(
                int(event[name])
                for name in (
                    "concise_delta",
                    "warm_delta",
                    "teasing_delta",
                    "proactive_delta",
                    "catchphrase_delta",
                    "direct_delta",
                )
            )
            values = tuple(
                _clip(value - delta)
                for value, delta in zip(_style_values(old), deltas, strict=True)
            )
            transaction.write(
                """UPDATE persona_style_profiles SET concise=?, warm=?, teasing=?,
                   proactive=?, catchphrase=?, direct=?, updated_at=?
                   WHERE owner_user_id=? AND scope_group_id=?""",
                (*values, timestamp, scope.owner_user_id, scope.group_id),
            )
            transaction.write(
                """DELETE FROM persona_feedback_events
                   WHERE id=? AND owner_user_id=? AND scope_group_id=?""",
                (int(event["id"]), scope.owner_user_id, scope.group_id),
            )
            return PersonaStyleProfile(
                scope, profile.learning_enabled, PersonaStyleContext(*values), timestamp
            )

        return await self.database.transaction(undo_last)

    async def reset(self, scope: PersonaScope) -> PersonaStyleProfile:
        timestamp = int(time.time())

        def reset_values(transaction: Transaction) -> PersonaStyleProfile:
            rows = transaction.read(
                """SELECT learning_enabled FROM persona_style_profiles
                   WHERE owner_user_id=? AND scope_group_id=?""",
                (scope.owner_user_id, scope.group_id),
            )
            enabled = bool(int(rows[0]["learning_enabled"])) if rows else False
            transaction.write(
                """INSERT INTO persona_style_profiles
                   (owner_user_id, scope_group_id, learning_enabled, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(owner_user_id, scope_group_id) DO UPDATE SET
                     concise=0, warm=0, teasing=0, proactive=0, catchphrase=0,
                     direct=0, updated_at=excluded.updated_at""",
                (scope.owner_user_id, scope.group_id, int(enabled), timestamp),
            )
            transaction.write(
                """DELETE FROM persona_feedback_events
                   WHERE owner_user_id=? AND scope_group_id=?""",
                (scope.owner_user_id, scope.group_id),
            )
            return PersonaStyleProfile(scope, enabled, PersonaStyleContext(), timestamp)

        return await self.database.transaction(reset_values)


__all__ = [
    "FEEDBACK_RETENTION_SECONDS",
    "MAX_FEEDBACK_PER_SCOPE",
    "PersonaStyleDelta",
    "PersonaStyleProfile",
    "PersonaStyleRepository",
    "is_ambiguous_style_feedback",
    "parse_style_feedback",
]
