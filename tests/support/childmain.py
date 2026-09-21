"""Fixed child entry for controlled test subprocesses.

Only this module may run as a test child process::

    python -m tests.support.childmain <task> <payload_file>

It installs the isolation guards *before* any application import, runs
one registered task, and prints a single JSON envelope with the task
result plus the child's own audit trail.  Per-test ``-c`` snippets must
not install guards or declare safety themselves; the parent merges the
child audit into its own log, so a denial swallowed inside the child
still fails the owning test.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

TASKS = ("status", "config-probe", "smoke", "swallow-probe")


def _status() -> dict:
    from tests.support import isolation

    return isolation.verify_child_contract()


def _config_probe(payload: dict) -> dict:
    from pathlib import Path as _Path

    from src.core.config import load_runtime_settings
    from tests.support import isolation

    outcome: dict = {}
    try:
        load_runtime_settings(_Path(str(payload["target"])))
        outcome["read"] = "unexpectedly-allowed"
    except Exception as exc:
        outcome["read"] = type(exc).__name__
    outcome["status"] = isolation.verify_child_contract()
    return outcome


def _smoke(payload: dict) -> dict:
    import bot
    from src.state.runtime import database, initialize_state, shutdown_state
    from tests.support import isolation

    for key in ("TAFFY_STATE_DB", "TAFFY_LOG_DIR", "TAFFY_SCHEDULE_CONFIG", "ENVIRONMENT"):
        value = payload.get("env", {}).get(key)
        if value is not None:
            os.environ[key] = str(value)
    status = isolation.verify_child_contract()

    async def run() -> dict:
        await initialize_state()
        ready = await database.healthcheck()
        plugins = sorted(bot._PLUGINS)
        database_path = str(database.path.resolve())
        await shutdown_state()
        return {"smoke_ready": ready, "plugins": len(plugins), "database_path": database_path}

    status.update(asyncio.run(run()))
    return status


def _swallow_probe(payload: dict) -> dict:
    """Deliberately swallow a denial: the parent merge must still catch it."""
    from tests.support import isolation

    try:
        Path(str(payload["victim"])).read_text(encoding="utf-8")
        return {"swallowed": False, "note": "read unexpectedly allowed"}
    except isolation.IsolationViolationError:
        return {"swallowed": True}
    except OSError as exc:
        return {"swallowed": False, "note": f"other error: {type(exc).__name__}"}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in TASKS:
        print(json.dumps({"error": f"usage: childmain <{'|'.join(TASKS)}> <payload>"}))
        return 2
    _task, payload_file = argv[1], argv[2]
    try:
        payload = json.loads(Path(payload_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": f"bad payload: {type(exc).__name__}"}))
        return 2
    from tests.support import isolation

    try:
        isolation.install_child_guards(sentinels=payload.get("sentinels", []))
    except isolation.IsolationViolationError as exc:
        print(json.dumps({"error": f"isolation refused: {exc}"}))
        return 3
    handlers = {
        "status": _status,
        "config-probe": _config_probe,
        "smoke": _smoke,
        "swallow-probe": _swallow_probe,
    }
    try:
        if _task == "status":
            result = handlers[_task]()
        else:
            result = handlers[_task](payload)
    except Exception as exc:  # task bugs are evidence, not silence
        print(json.dumps({"error": f"task failed: {type(exc).__name__}: {exc}"}))
        return 1
    envelope = {"result": result, "audit": isolation.audit_events()}
    print(json.dumps(envelope, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
