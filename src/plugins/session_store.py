"""Compatibility facade for the SQLite-backed conversation repository."""

from src.state.runtime import sessions as store
from src.state.session_repository import (
    MAX_MESSAGE_CHARS as _MAX_MESSAGE_CHARS,
)
from src.state.session_repository import (
    MAX_MESSAGES as _MAX_MESSAGES,
)
from src.state.session_repository import (
    MAX_SESSION_CHARS as _MAX_SESSION_CHARS,
)
from src.state.session_repository import SessionRepository

# Historical imports used ``SessionStore`` and counted user/assistant pairs as
# turns.  Keep names only; persistence is now immediate SQLite transactions.
SessionStore = SessionRepository
_MAX_TURNS = _MAX_MESSAGES // 2

__all__ = [
    "SessionStore",
    "_MAX_MESSAGE_CHARS",
    "_MAX_SESSION_CHARS",
    "_MAX_TURNS",
    "store",
]
