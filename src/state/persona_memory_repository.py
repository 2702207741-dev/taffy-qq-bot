"""Explicit, scope-isolated long-term persona memories."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, cast

from src.core.database import Transaction
from src.services.persona import PersonaContext

from .repository import Repository, nonnegative_int, positive_int

MemoryKind = Literal["nickname", "note"]
MAX_MEMORIES_PER_SCOPE = 20
MAX_MEMORY_CHARS = 200
MAX_NICKNAME_CHARS = 20
MAX_PROMPT_MEMORIES = 12
MAX_PROMPT_MEMORY_CHARS = 1_500


class PersonaMemoryCapacityError(ValueError):
    """The current private/group scope already reached its hard limit."""


@dataclass(frozen=True, slots=True)
class PersonaScope:
    owner_user_id: int
    group_id: int = 0

    def __post_init__(self) -> None:
        positive_int(self.owner_user_id, name="owner_user_id")
        nonnegative_int(self.group_id, name="group_id")

    @property
    def label(self) -> str:
        return "私聊" if self.group_id == 0 else "当前群聊"


@dataclass(frozen=True, slots=True)
class PersonaMemory:
    id: int
    scope: PersonaScope
    kind: MemoryKind
    content: str
    created_at: int
    updated_at: int


def _clean_content(content: str, *, maximum: int) -> tuple[str, str]:
    value = unicodedata.normalize("NFKC", str(content))
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        raise ValueError("记忆内容不能为空")
    if len(value) > maximum:
        raise ValueError(f"记忆内容不能超过{maximum}个字")
    return value, value.casefold()


def _memory_from_row(row: dict[str, object]) -> PersonaMemory:
    kind = str(row["memory_kind"])
    if kind not in {"nickname", "note"}:
        raise ValueError("stored memory kind is invalid")
    integer_fields = ("id", "owner_user_id", "scope_group_id", "created_at", "updated_at")
    if any(
        isinstance(row[field], bool) or not isinstance(row[field], int) for field in integer_fields
    ):
        raise ValueError("stored memory integer field is invalid")
    return PersonaMemory(
        id=cast(int, row["id"]),
        scope=PersonaScope(
            cast(int, row["owner_user_id"]),
            cast(int, row["scope_group_id"]),
        ),
        kind=cast(MemoryKind, kind),
        content=str(row["content"]),
        created_at=cast(int, row["created_at"]),
        updated_at=cast(int, row["updated_at"]),
    )


class PersonaMemoryRepository(Repository):
    """Persist only user-requested memories within one derived QQ scope."""

    async def remember_note(self, scope: PersonaScope, content: str) -> tuple[PersonaMemory, bool]:
        clean, normalized = _clean_content(content, maximum=MAX_MEMORY_CHARS)

        def remember(transaction: Transaction) -> tuple[PersonaMemory, bool]:
            existing = transaction.read(
                """
                SELECT * FROM persona_memories
                WHERE owner_user_id = ? AND scope_group_id = ?
                  AND memory_kind = 'note' AND normalized_content = ?
                """,
                (scope.owner_user_id, scope.group_id, normalized),
            )
            if existing:
                return _memory_from_row(existing[0]), False
            count = transaction.read(
                """
                SELECT COUNT(*) AS count FROM persona_memories
                WHERE owner_user_id = ? AND scope_group_id = ?
                """,
                (scope.owner_user_id, scope.group_id),
            )[0]
            if int(count["count"]) >= MAX_MEMORIES_PER_SCOPE:
                raise PersonaMemoryCapacityError("当前范围的长期记忆已满")
            result = transaction.write(
                """
                INSERT INTO persona_memories (
                    owner_user_id, scope_group_id, memory_kind, content, normalized_content
                ) VALUES (?, ?, 'note', ?, ?)
                """,
                (scope.owner_user_id, scope.group_id, clean, normalized),
            )
            rows = transaction.read(
                "SELECT * FROM persona_memories WHERE id = ?",
                (result.lastrowid,),
            )
            return _memory_from_row(rows[0]), True

        return await self.database.transaction(remember)

    async def set_nickname(self, scope: PersonaScope, nickname: str) -> PersonaMemory:
        clean, normalized = _clean_content(nickname, maximum=MAX_NICKNAME_CHARS)

        def replace(transaction: Transaction) -> PersonaMemory:
            current = transaction.read(
                """
                SELECT * FROM persona_memories
                WHERE owner_user_id = ? AND scope_group_id = ? AND memory_kind = 'nickname'
                """,
                (scope.owner_user_id, scope.group_id),
            )
            if current and str(current[0]["normalized_content"]) == normalized:
                return _memory_from_row(current[0])
            if not current:
                count = transaction.read(
                    """
                    SELECT COUNT(*) AS count FROM persona_memories
                    WHERE owner_user_id = ? AND scope_group_id = ?
                    """,
                    (scope.owner_user_id, scope.group_id),
                )[0]
                if int(count["count"]) >= MAX_MEMORIES_PER_SCOPE:
                    raise PersonaMemoryCapacityError("当前范围的长期记忆已满")
            transaction.write(
                """
                DELETE FROM persona_memories
                WHERE owner_user_id = ? AND scope_group_id = ? AND memory_kind = 'nickname'
                """,
                (scope.owner_user_id, scope.group_id),
            )
            result = transaction.write(
                """
                INSERT INTO persona_memories (
                    owner_user_id, scope_group_id, memory_kind, content, normalized_content
                ) VALUES (?, ?, 'nickname', ?, ?)
                """,
                (scope.owner_user_id, scope.group_id, clean, normalized),
            )
            rows = transaction.read(
                "SELECT * FROM persona_memories WHERE id = ?",
                (result.lastrowid,),
            )
            return _memory_from_row(rows[0])

        return await self.database.transaction(replace)

    async def list(
        self, scope: PersonaScope, *, limit: int = MAX_MEMORIES_PER_SCOPE
    ) -> tuple[PersonaMemory, ...]:
        bounded_limit = max(1, min(int(limit), MAX_MEMORIES_PER_SCOPE))
        rows = await self.database.read(
            """
            SELECT * FROM persona_memories
            WHERE owner_user_id = ? AND scope_group_id = ?
            ORDER BY CASE memory_kind WHEN 'nickname' THEN 0 ELSE 1 END,
                     updated_at DESC, id DESC
            LIMIT ?
            """,
            (scope.owner_user_id, scope.group_id, bounded_limit),
        )
        return tuple(_memory_from_row(row) for row in rows)

    async def update_note(
        self, scope: PersonaScope, memory_id: int, content: str
    ) -> PersonaMemory | None:
        """Rewrite one existing note in place (memory-update decision).

        Used by the mem0-style write path when new information refines an
        older memory instead of contradicting or duplicating it.  Nickname
        rows keep their dedicated replacement flow.
        """

        clean, normalized = _clean_content(content, maximum=MAX_MEMORY_CHARS)

        def update(transaction: Transaction) -> PersonaMemory | None:
            rows = transaction.read(
                """
                SELECT * FROM persona_memories
                WHERE id = ? AND owner_user_id = ? AND scope_group_id = ?
                  AND memory_kind = 'note'
                """,
                (memory_id, scope.owner_user_id, scope.group_id),
            )
            if not rows:
                return None
            transaction.write(
                """
                UPDATE persona_memories
                SET content = ?, normalized_content = ?, updated_at =
                    CAST(strftime('%s', 'now') AS INTEGER)
                WHERE id = ? AND owner_user_id = ? AND scope_group_id = ?
                  AND memory_kind = 'note'
                """,
                (clean, normalized, memory_id, scope.owner_user_id, scope.group_id),
            )
            updated = transaction.read("SELECT * FROM persona_memories WHERE id = ?", (memory_id,))
            return _memory_from_row(updated[0]) if updated else None

        return await self.database.transaction(update)

    async def delete_at(self, scope: PersonaScope, position: int) -> PersonaMemory | None:
        if isinstance(position, bool) or not isinstance(position, int) or position <= 0:
            raise ValueError("记忆序号必须是正整数")

        def delete(transaction: Transaction) -> PersonaMemory | None:
            rows = transaction.read(
                """
                SELECT * FROM persona_memories
                WHERE owner_user_id = ? AND scope_group_id = ?
                ORDER BY CASE memory_kind WHEN 'nickname' THEN 0 ELSE 1 END,
                         updated_at DESC, id DESC
                LIMIT 1 OFFSET ?
                """,
                (scope.owner_user_id, scope.group_id, position - 1),
            )
            if not rows:
                return None
            memory = _memory_from_row(rows[0])
            result = transaction.write(
                """
                DELETE FROM persona_memories
                WHERE id = ? AND owner_user_id = ? AND scope_group_id = ?
                """,
                (memory.id, scope.owner_user_id, scope.group_id),
            )
            return memory if result.rowcount == 1 else None

        return await self.database.transaction(delete)

    async def clear(self, scope: PersonaScope) -> int:
        result = await self.database.write(
            """
            DELETE FROM persona_memories
            WHERE owner_user_id = ? AND scope_group_id = ?
            """,
            (scope.owner_user_id, scope.group_id),
        )
        return max(0, result.rowcount)

    async def clear_all(self, scope: PersonaScope, session_key: str) -> tuple[int, int]:
        """Atomically clear recent chat and long-term memory for one QQ scope."""

        expected_key = (
            f"p_{scope.owner_user_id}"
            if scope.group_id == 0
            else f"g_{scope.group_id}_{scope.owner_user_id}"
        )
        if session_key != expected_key:
            raise ValueError("session key does not match persona scope")

        def clear(transaction: Transaction) -> tuple[int, int]:
            session_result = transaction.write(
                "DELETE FROM session_messages WHERE session_key = ?",
                (session_key,),
            )
            memory_result = transaction.write(
                """
                DELETE FROM persona_memories
                WHERE owner_user_id = ? AND scope_group_id = ?
                """,
                (scope.owner_user_id, scope.group_id),
            )
            return max(0, session_result.rowcount), max(0, memory_result.rowcount)

        return await self.database.transaction(clear)

    async def context(self, scope: PersonaScope) -> PersonaContext:
        memories = await self.list(scope)
        nickname = next((item.content for item in memories if item.kind == "nickname"), None)
        selected: list[str] = []
        used = 0
        for memory in memories:
            if memory.kind == "nickname":
                continue
            if len(selected) >= MAX_PROMPT_MEMORIES:
                break
            addition = len(memory.content)
            if used + addition > MAX_PROMPT_MEMORY_CHARS:
                break
            selected.append(memory.content)
            used += addition
        return PersonaContext(preferred_name=nickname, memories=tuple(selected))


__all__ = [
    "MAX_MEMORIES_PER_SCOPE",
    "MAX_MEMORY_CHARS",
    "MAX_NICKNAME_CHARS",
    "MAX_PROMPT_MEMORIES",
    "MAX_PROMPT_MEMORY_CHARS",
    "PersonaMemory",
    "PersonaMemoryCapacityError",
    "PersonaMemoryRepository",
    "PersonaScope",
]
