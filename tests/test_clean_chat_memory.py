from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.clean_chat_memory import clean_active_memory, inspect_memory
from src.core.database import StateDatabase


@pytest.mark.asyncio
async def test_cleanup_backs_up_then_removes_only_chat_memory(tmp_path: Path) -> None:
    database_path = tmp_path / "taffy.db"
    database = StateDatabase(database_path)
    await database.initialize()
    await database.write("INSERT INTO app_meta (key, value) VALUES ('keep-me', 'still-here')")
    await database.write(
        """
        INSERT INTO session_messages (session_key, role, content)
        VALUES ('private:1', 'user', 'question'),
               ('private:1', 'assistant', 'unsupported answer')
        """
    )
    legacy_path = tmp_path / "sessions.json"
    legacy_path.write_text(
        json.dumps({"private:1": [{"role": "assistant", "content": "legacy unsupported answer"}]}),
        encoding="utf-8",
    )

    report = clean_active_memory(
        database_path,
        legacy_path=legacy_path,
        backup_dir=tmp_path / "backups",
    )

    assert report["before"] == {
        "messages": 2,
        "sessions": 1,
        "characters": len("questionunsupported answer"),
        "assistant_messages": 1,
    }
    assert report["after"]["messages"] == 0
    assert inspect_memory(database_path)["messages"] == 0
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == {}

    backup_path = Path(report["database_backup"]["path"])
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT COUNT(*) FROM session_messages").fetchone() == (2,)
        assert backup.execute("SELECT value FROM app_meta WHERE key = 'keep-me'").fetchone() == (
            "still-here",
        )

    with sqlite3.connect(database_path) as active:
        assert active.execute("SELECT value FROM app_meta WHERE key = 'keep-me'").fetchone() == (
            "still-here",
        )

    legacy_backup = Path(report["legacy_backup"]["path"])
    assert "legacy unsupported answer" in legacy_backup.read_text(encoding="utf-8")
